"""Optional recording of approved Douyin CDN streams using the system FFmpeg.

Nothing is recorded until both room switches are explicitly true.  Credentials
and stream URLs are never included in returned status or FFmpeg error messages.
The CDN allowlist is the trust boundary for redirects and HLS child playlists;
the initial URL and every address returned by its DNS lookup must be public.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


CDN_DOMAINS = (
    "douyincdn.com", "douyinvod.com", "bytecdn.cn", "pstatp.com",
    "ibytedtos.com", "byteimg.com",
)
MIN_FREE_BYTES = 512 * 1024 * 1024
DISK_CHECK_SECONDS = 5.0
STARTUP_TIMEOUT_SECONDS = 30.0
RETRY_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0
_ROOM_ID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")


@dataclass
class _Recording:
    state: str = "idle"
    file: str | None = None
    started_at: str | None = None
    error: str | None = None
    process: asyncio.subprocess.Process | None = None
    monitor: asyncio.Task | None = None
    stderr_reader: asyncio.Task | None = None
    stopping: bool = False
    retry_at: float = 0.0


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    # Validate IPv4-mapped IPv6 addresses using their actual IPv4 destination.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def _validate_stream_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise ValueError("直播流地址为空或格式无效。")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError("直播流地址包含非法字符。")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("直播流地址格式无效。") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅支持 HTTP 或 HTTPS 直播流。")
    expected_port = 443 if parsed.scheme == "https" else 80
    if port not in {None, expected_port} or parsed.username is not None or parsed.password is not None:
        raise ValueError("直播流地址不允许自定义端口或用户信息。")
    if parsed.fragment or "\\" in value or not parsed.path or parsed.path == "/":
        raise ValueError("直播流地址格式无效。")
    if not any(host == domain or host.endswith("." + domain) for domain in CDN_DOMAINS):
        raise ValueError("直播流地址不在已允许的抖音 CDN 域名中。")
    try:
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host, expected_port, type=socket.SOCK_STREAM,
            ),
            timeout=5.0,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ValueError("无法确认直播流的公网地址，请稍后重试。") from exc
    if not addresses or not all(_public_address(item[4][0]) for item in addresses):
        raise ValueError("直播流地址必须解析到公网，已拒绝本机或内网地址。")
    return value


class Recorder:
    def __init__(self, data_dir: Path, require_selection=False):
        self.require_selection = require_selection
        self.folder_selected = False
        self.data_dir = Path(data_dir).resolve()
        self.recordings_dir = self.data_dir / "recordings"
        self._entries: dict[str, _Recording] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._stop_generations: dict[str, int] = {}
        self._closed = False
        self.contexts = {}

    def _lock(self, room_id: str) -> asyncio.Lock:
        return self._locks.setdefault(room_id, asyncio.Lock())

    def status(self, room_id: str) -> dict:
        entry = self._entries.get(str(room_id))
        if entry is None:
            return {"state": "idle", "file": None, "started_at": None, "error": None}
        # Do not report recording between process exit and the monitor's tick.
        if entry.process is not None and entry.process.returncode is not None and not entry.stopping:
            entry.state = "error"
            entry.error = f"FFmpeg 已退出（代码 {entry.process.returncode}）。"
        return {
            "state": entry.state, "file": entry.file,
            "started_at": entry.started_at, "error": entry.error,
        }

    async def sync(self, room: dict, snapshot: dict) -> dict:
        room_id = str(room.get("id", ""))
        if not _ROOM_ID.fullmatch(room_id):
            return {"state": "error", "file": None, "started_at": None, "error": "直播间标识无效。"}
        state = snapshot.get("status")
        should_stop = room.get("enabled") is not True or room.get("record_enabled") is not True or state == "offline"
        if should_stop:
            self._stop_generations[room_id] = self._stop_generations.get(room_id, 0) + 1
        generation = self._stop_generations.get(room_id, 0)
        async with self._lock(room_id):
            entry = self._entries.setdefault(room_id, _Recording())
            self.contexts[room_id] = dict(room)
            # A queued observation must not restart a recording after a newer stop.
            if generation != self._stop_generations.get(room_id, 0):
                return self.status(room_id)
            if self._closed:
                return self._fail(entry, "录制服务已关闭。")
            if should_stop:
                await self._stop_entry(entry)
                return self.status(room_id)
            if state != "live":
                # Collection failures are not proof that a healthy stream ended.
                return self.status(room_id)
            if entry.process is not None and entry.process.returncode is None:
                return self.status(room_id)
            if entry.process is not None:
                await self._stop_entry(entry, error="上一次录制已经退出，稍后重试。")
            if time.monotonic() < entry.retry_at:
                return self.status(room_id)
            try:
                if self.require_selection and not self.folder_selected:
                    raise ValueError("首次录制前必须选择视频保存文件夹。")
                stream_url = await _validate_stream_url(snapshot.get("stream_url"))
                ffmpeg = await asyncio.to_thread(shutil.which, "ffmpeg")
                if not ffmpeg:
                    raise ValueError("系统未找到 FFmpeg，请先安装并加入 PATH。")
                output = await asyncio.to_thread(self._prepare_output, room_id)
                if not await self._has_space():
                    raise ValueError("磁盘剩余空间不足 512 MB，录制已停止。")
                self.output_prepared(room_id, output)
            except (ValueError, OSError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "无法创建录制目录，请检查目录权限和磁盘。"
                return self._fail(entry, message)
            if self._closed or generation != self._stop_generations.get(room_id, 0):
                return self.status(room_id)

            args = self._ffmpeg_args(ffmpeg, stream_url, output)
            options = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": asyncio.subprocess.DEVNULL,
                "stderr": asyncio.subprocess.PIPE,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            start_task = asyncio.create_task(asyncio.create_subprocess_exec(*args, **options))
            try:
                process = await asyncio.shield(start_task)
            except asyncio.CancelledError:
                # App shutdown may cancel sync exactly while FFmpeg is spawning.
                # Keep the spawn result reachable and stop it before propagating.
                try:
                    process = await start_task
                    entry.process = process
                    entry.file = str(output)
                    entry.stderr_reader = asyncio.create_task(self._drain_stderr(process))
                    await self._stop_entry(entry)
                except (OSError, ValueError, NotImplementedError):
                    pass
                raise
            except (OSError, ValueError, NotImplementedError):
                return self._fail(entry, "FFmpeg 无法启动，请检查系统安装和运行权限。")

            entry.process = process
            entry.state = "idle"  # Becomes recording only after output is written.
            entry.file = str(output)
            entry.started_at = None
            entry.error = None
            entry.stopping = False
            entry.stderr_reader = asyncio.create_task(self._drain_stderr(process))
            if self._closed or generation != self._stop_generations.get(room_id, 0):
                await self._stop_entry(entry)
                return self.status(room_id)
            entry.monitor = asyncio.create_task(self._watch(room_id, entry, process))
            return self.status(room_id)

    def output_prepared(self, room_id, output):
        """Event-loop hook after filesystem preparation; safe for SQLite writes."""
        pass

    def _ffmpeg_args(self, ffmpeg: str, stream_url: str, output: Path) -> tuple:
        return (ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
                "-protocol_whitelist", "http,https,tcp,tls,crypto",
                "-rw_timeout", "15000000", "-i", stream_url,
                "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                "-f", "mpegts", "-n", str(output))

    def _prepare_output(self, room_id: str) -> Path:
        base = self.recordings_dir.resolve()
        directory = (base / room_id).resolve()
        if not directory.is_relative_to(base):
            raise ValueError("录制目录不能指向数据目录之外。")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        return directory / f"{stamp}.ts"

    async def _has_space(self) -> bool:
        usage = await asyncio.to_thread(shutil.disk_usage, self.recordings_dir)
        return usage.free >= MIN_FREE_BYTES

    @staticmethod
    def _fail(entry: _Recording, message: str) -> dict:
        entry.state = "error"
        entry.error = message
        entry.retry_at = time.monotonic() + RETRY_SECONDS
        return {"state": entry.state, "file": entry.file, "started_at": entry.started_at, "error": entry.error}

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
        if process.stderr is not None:
            while await process.stderr.read(4096):
                pass

    @staticmethod
    def _has_output(filename: str) -> bool:
        try:
            return Path(filename).stat().st_size > 0
        except OSError:
            return False

    async def _watch(self, room_id: str, entry: _Recording, process: asyncio.subprocess.Process) -> None:
        began = time.monotonic()
        disk_check_at = 0.0
        try:
            while entry.process is process and not entry.stopping:
                if process.returncode is not None:
                    async with self._lock(room_id):
                        if entry.process is process and not entry.stopping:
                            await self._stop_entry(entry, error=f"FFmpeg 已退出（代码 {process.returncode}）。")
                    return
                now = time.monotonic()
                if now >= disk_check_at:
                    try:
                        enough_space = await self._has_space()
                    except OSError:
                        enough_space = False
                    if not enough_space:
                        async with self._lock(room_id):
                            if entry.process is process and not entry.stopping:
                                await self._stop_entry(entry, error="磁盘剩余空间不足 512 MB 或磁盘不可用，录制已停止。")
                        return
                    disk_check_at = now + DISK_CHECK_SECONDS
                if entry.state == "idle":
                    has_output = await asyncio.to_thread(self._has_output, entry.file)
                    if has_output and process.returncode is None:
                        entry.state = "recording"
                        entry.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    elif now - began >= STARTUP_TIMEOUT_SECONDS:
                        async with self._lock(room_id):
                            if entry.process is process and not entry.stopping:
                                await self._stop_entry(entry, error="FFmpeg 启动后未收到可写入的视频数据，请检查直播流。")
                        return
                await asyncio.sleep(0.25 if entry.state == "idle" else 1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock(room_id):
                if entry.process is process and not entry.stopping:
                    await self._stop_entry(entry, error="录制监测失败，已停止录制。")

    async def stop(self, room_id: str) -> dict:
        room_id = str(room_id)
        # Invalidate work before waiting for a room lock or the child to exit.
        self._stop_generations[room_id] = self._stop_generations.get(room_id, 0) + 1
        async with self._lock(room_id):
            entry = self._entries.get(room_id)
            if entry is not None:
                await self._stop_entry(entry)
            return self.status(room_id)

    async def _stop_entry(self, entry: _Recording, error: str | None = None) -> None:
        entry.stopping = True
        current = asyncio.current_task()
        if entry.monitor is not None and entry.monitor is not current:
            entry.monitor.cancel()
            await asyncio.gather(entry.monitor, return_exceptions=True)
        process = entry.process
        if process is not None and process.returncode is None:
            try:
                if process.stdin is not None:
                    process.stdin.write(b"q\n")
                    await asyncio.wait_for(process.stdin.drain(), timeout=1.0)
                await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT_SECONDS)
            except (BrokenPipeError, ConnectionError, OSError, asyncio.TimeoutError):
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await process.wait()
        if entry.stderr_reader is not None:
            entry.stderr_reader.cancel()
            await asyncio.gather(entry.stderr_reader, return_exceptions=True)
        entry.process = None
        entry.monitor = None
        entry.stderr_reader = None
        entry.stopping = False
        entry.state = "error" if error else "idle"
        entry.error = error
        entry.retry_at = time.monotonic() + RETRY_SECONDS if error else 0.0

    async def close(self) -> None:
        self._closed = True
        await asyncio.gather(*(self.stop(room_id) for room_id in list(self._entries)))
