"""Opt-in live audio segmentation and local, persistent speech transcripts."""
from __future__ import annotations
import asyncio
import contextlib
import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

from recorder import Recorder

CHUNK_SECONDS = 30


class AudioRecorder(Recorder):
    def __init__(self, data_dir):
        super().__init__(data_dir)
        self.recordings_dir = self.data_dir / "transcription-audio"

    def _prepare_output(self, room_id):
        anchor = datetime.now(timezone.utc)
        folder = (self.recordings_dir / room_id / anchor.strftime("%Y%m%d-%H%M%S-%f")).resolve()
        if not folder.is_relative_to(self.recordings_dir.resolve()):
            raise ValueError("音频保存目录无效")
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "session.json").write_text(json.dumps({"room_id":room_id,"session_id":folder.name,"capture_started_at":anchor.isoformat(),"broadcast_id":self.contexts.get(room_id,{}).get("broadcast_id")},ensure_ascii=False),encoding="utf-8")
        return folder / "chunk-%06d.wav"

    def _ffmpeg_args(self, ffmpeg, stream_url, output):
        return (ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
                "-protocol_whitelist", "http,https,tcp,tls,crypto",
                "-rw_timeout", "15000000", "-i", stream_url,
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
                "-segment_list", str(output.parent / "segments.csv"), "-segment_list_type", "csv", "-n", str(output))

    @staticmethod
    def _has_output(filename):
        return any(p.stat().st_size > 44 for p in Path(filename).parent.glob("chunk-*.wav"))


class SpeechService:
    def __init__(self, data_dir, store):
        self.store = store
        self.audio = AudioRecorder(Path(data_dir))
        self.worker = None
        self.stderr_task = None
        self.task = None
        self.closed = False
        self.active = None
        self.last_errors = {}
        self.scan_marks = {}
        self.last_scan = 0

    def status(self, rid):
        audio = self.audio.status(rid)
        capture_error = audio.get("error")
        if capture_error == "直播流地址为空或格式无效。":
            capture_error = "已检测到开播，但暂未取得音频地址，程序会继续尝试解析。"
        row = self.store.db.execute("SELECT COUNT(*) AS chunks, SUM(status='done') AS done, SUM(status='silent') AS silent, SUM(status IN ('pending','processing')) AS pending, MAX(completed_at) AS last_completed FROM speech_chunks WHERE room_id=?", (rid,)).fetchone()
        counts = dict(row)
        for field in ("chunks","done","silent","pending"):
            counts[field] = counts[field] or 0
        return {"audio_state":audio["state"],"processing":self.active == rid,"error":self.last_errors.get(rid) or capture_error,"chunk_seconds":CHUNK_SECONDS,"engine":"Whisper small · 本地 CPU","counts":counts}

    async def sync(self, room, snapshot):
        previous=self.audio.contexts.get(room['id'],{}).get('broadcast_id')
        if previous and previous!=room.get('broadcast_id'):
            await self.stop(room['id'])
        return await self.audio.sync(room | {"record_enabled":bool(room.get("transcribe_enabled"))}, snapshot)

    async def stop(self, rid):
        await self.audio.stop(rid)
        self.scan_marks.clear()  # FFmpeg writes the final closed segment on stop.

    def scan(self):
        """Only CSV-listed, closed segments are queued; never read a growing WAV."""
        base = self.audio.recordings_dir
        for manifest in base.glob("*/*/session.json"):
            index = manifest.with_name("segments.csv")
            try:
                stamp = index.stat().st_mtime_ns
                if self.scan_marks.get(str(index)) == stamp:
                    continue
                meta = json.loads(manifest.read_text(encoding="utf-8"))
                rid = meta["room_id"]
                if not self.store.get(rid):
                    continue
                anchor = datetime.fromisoformat(meta["capture_started_at"])
                with index.open(newline="",encoding="utf-8-sig") as handle:
                    rows = list(csv.reader(handle))
                for row in rows:
                    if len(row) != 3:
                        continue
                    wav = (index.parent / row[0]).resolve()
                    if wav.parent != index.parent.resolve() or wav.suffix != ".wav" or not wav.is_file():
                        continue
                    start,end = float(row[1]),float(row[2])
                    if not (0 <= start < end) or end-start > CHUNK_SECONDS+2 or wav.stat().st_size <= 44:
                        continue
                    captured = (anchor + timedelta(seconds=start)).isoformat(timespec="milliseconds")
                    self.store.db.execute("INSERT OR IGNORE INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,broadcast_id,status) VALUES(?,?,?,?,?,?,?,'pending')",(rid,meta["session_id"],str(wav),start,end,captured,meta.get("broadcast_id")))
                self.store.db.commit()
                self.scan_marks[str(index)] = stamp
            except (OSError, ValueError, KeyError, csv.Error):
                continue

    async def _ensure_worker(self):
        if self.worker and self.worker.returncode is None:
            return self.worker
        args = [sys.executable,"-B","-u",str(Path(__file__).with_name("asr_worker.py"))]
        options = {"stdin":asyncio.subprocess.PIPE,"stdout":asyncio.subprocess.PIPE,"stderr":asyncio.subprocess.PIPE}
        if hasattr(subprocess,"CREATE_NO_WINDOW"):
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(*args,**options))
        try:
            self.worker = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        async def drain(process):
            while await process.stderr.read(4096):
                pass
        self.stderr_task = asyncio.create_task(drain(self.worker))
        return self.worker

    async def _kill_worker(self):
        if self.worker and self.worker.returncode is None:
            self.worker.kill()
            await self.worker.wait()
        self.worker = None
        if self.stderr_task:
            self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task,return_exceptions=True)
            self.stderr_task = None

    async def run(self):
        while not self.closed:
            self.scan()
            row = self.store.db.execute("SELECT * FROM speech_chunks WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if not row:
                await asyncio.sleep(1)
                continue
            job = dict(row)
            self.active = job["room_id"]
            self.store.db.execute("UPDATE speech_chunks SET status='processing' WHERE id=?",(job["id"],))
            self.store.db.commit()
            try:
                worker = await self._ensure_worker()
                worker.stdin.write((json.dumps({"path":job["audio_path"]})+"\n").encode("utf-8"))
                await worker.stdin.drain()
                line = await asyncio.wait_for(worker.stdout.readline(),timeout=120)
                if not line:
                    raise RuntimeError("本地转写进程退出，请关闭再开启转写重试")
                result = json.loads(line)
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "本地语音识别未完成")
                segments = result.get("segments") or []
                text = "".join(str(seg["text"]).strip() for seg in segments)
                status = "done" if text else "silent"
                self.store.db.execute("UPDATE speech_chunks SET status=?,text=?,segments=?,model=?,elapsed_seconds=?,completed_at=?,error=NULL WHERE id=?",(status,text,json.dumps(segments,ensure_ascii=False),result.get("model"),result.get("elapsed_seconds"),datetime.now(timezone.utc).isoformat(timespec="seconds"),job["id"]))
                self.store.db.commit()
                self.last_errors.pop(job["room_id"],None)
            except asyncio.CancelledError:
                self.store.db.execute("UPDATE speech_chunks SET status='pending' WHERE id=? AND status='processing'",(job["id"],))
                self.store.db.commit()
                raise
            except Exception as exc:
                message = str(exc)[:300] or "本地转写超时，请重试"
                self.store.db.execute("UPDATE speech_chunks SET status='error',error=? WHERE id=?",(message,job["id"]))
                self.store.db.commit()
                if self.last_errors.get(job["room_id"]) != message and self.store.get(job["room_id"]):
                    self.store.event(job["room_id"],"transcription_error",message)
                self.last_errors[job["room_id"]] = message
                await self._kill_worker()
                await asyncio.sleep(2)
            finally:
                self.active = None

    async def close(self):
        self.closed = True
        await self.audio.close()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        await self._kill_worker()
        self.scan_marks.clear()
        self.scan()  # Retain the flushed tail for the next service launch.
