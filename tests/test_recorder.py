import asyncio
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import recorder


PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
LIVE = {"status": "live", "stream_url": "https://pull-flv-l11.douyincdn.com/live/sample.flv?token=private"}
ROOM = {"id": "test-room", "name": "测试直播间", "enabled": True, "record_enabled": True}


class _Input:
    def __init__(self, process):
        self.process = process
        self.written = []

    def write(self, value):
        self.written.append(value)
        if value == b"q\n":
            self.process.finish(0)

    async def drain(self):
        pass


class _Process:
    def __init__(self):
        self.returncode = None
        self.stdin = _Input(self)
        self.stderr = asyncio.StreamReader()
        self._exited = asyncio.Event()

    def finish(self, returncode):
        if self.returncode is None:
            self.returncode = returncode
            self.stderr.feed_eof()
            self._exited.set()

    async def wait(self):
        await self._exited.wait()
        return self.returncode

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.recorder = recorder.Recorder(Path(self.directory.name))
        self.processes = []

        async def create_process(*args, **kwargs):
            process = _Process()
            self.processes.append(process)
            self.last_args = args
            self.last_options = kwargs
            return process

        self.spawn = AsyncMock(side_effect=create_process)
        self.patches = [
            patch.object(asyncio, "create_subprocess_exec", self.spawn),
            patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=PUBLIC_DNS)),
            patch.object(recorder.shutil, "which", return_value="system-ffmpeg"),
            patch.object(recorder.Recorder, "_has_space", AsyncMock(return_value=True)),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        await self.recorder.close()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    async def _start(self):
        await self.recorder.sync(ROOM, LIVE)
        self.assertEqual(self.spawn.await_count, 1)
        return self.processes[-1]

    async def test_recording_is_explicitly_opt_in(self):
        for changes in ({"enabled": False}, {"record_enabled": False}, {"record_enabled": "false"}):
            result = await self.recorder.sync(ROOM | changes, LIVE)
            self.assertEqual(result["state"], "idle")
        no_record_switch = {key: value for key, value in ROOM.items() if key != "record_enabled"}
        await self.recorder.sync(no_record_switch, LIVE)
        self.spawn.assert_not_awaited()

    async def test_no_start_for_unknown_or_offline(self):
        for status in ("unknown", "error", "blocked", "offline"):
            await self.recorder.sync(ROOM, LIVE | {"status": status})
        self.spawn.assert_not_awaited()

    async def test_pause_disable_and_offline_stop(self):
        for room, snapshot in (
            (ROOM | {"enabled": False}, LIVE),
            (ROOM | {"record_enabled": False}, LIVE),
            (ROOM, {"status": "offline"}),
        ):
            await self.recorder.sync(ROOM, LIVE)
            process = self.processes[-1]
            result = await self.recorder.sync(room, snapshot)
            self.assertEqual(result["state"], "idle")
            self.assertEqual(process.stdin.written, [b"q\n"])
            self.assertEqual(process.returncode, 0)

    async def test_transient_collection_errors_do_not_stop_process(self):
        process = await self._start()
        for status in ("unknown", "error", "blocked"):
            await self.recorder.sync(ROOM, {"status": status})
            self.assertIsNone(process.returncode)
            self.assertEqual(process.stdin.written, [])
        self.assertEqual(self.spawn.await_count, 1)

    async def test_rejects_illegal_stream_urls(self):
        urls = (
            "file:///C:/secret", "https://127.0.0.1/live.flv", "https://localhost/live.flv",
            "https://douyincdn.com.evil.test/live.flv", "https://evil.test/live.flv",
            "https://user:password@pull.douyincdn.com/live.flv",
            "https://pull.douyincdn.com:8080/live.flv", "http://pull.douyincdn.com:443/live.flv",
            "https://pull.douyincdn.com/live.flv\n", "https://pull.douyincdn.com/live.flv#fragment",
        )
        for index, url in enumerate(urls):
            with self.subTest(url=url):
                result = await self.recorder.sync(ROOM | {"id": f"invalid-{index}"}, {"status": "live", "stream_url": url})
                self.assertEqual(result["state"], "error")
                self.assertNotIn(url, result["error"])
        self.spawn.assert_not_awaited()

    async def test_rejects_private_dns_addresses(self):
        for index, address in enumerate(("127.0.0.1", "10.0.0.1", "192.168.1.2", "169.254.169.254", "::1", "::ffff:127.0.0.1")):
            answer = PUBLIC_DNS + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=answer)):
                result = await self.recorder.sync(ROOM | {"id": f"private-{index}"}, LIVE)
            self.assertEqual(result["state"], "error")
        self.spawn.assert_not_awaited()

    async def test_http_is_preserved_and_process_has_no_shell(self):
        stream = "http://pull-flv-l11.douyincdn.com/live/sample.flv"
        await self.recorder.sync(ROOM, {"status": "live", "stream_url": stream})
        self.assertEqual(self.last_args[self.last_args.index("-i") + 1], stream)
        self.assertEqual(self.last_args[self.last_args.index("-c") + 1], "copy")
        self.assertEqual(self.last_args[self.last_args.index("-protocol_whitelist") + 1], "http,https,tcp,tls,crypto")
        self.assertNotIn("shell", self.last_options)
        self.assertTrue(Path(self.last_args[-1]).is_relative_to(self.recorder.recordings_dir))
        self.assertEqual(Path(self.last_args[-1]).suffix, ".ts")
        if hasattr(recorder.subprocess, "CREATE_NO_WINDOW"):
            self.assertEqual(self.last_options["creationflags"], recorder.subprocess.CREATE_NO_WINDOW)

    async def test_recording_only_after_real_output(self):
        await self._start()
        self.assertEqual(self.recorder.status(ROOM["id"])["state"], "idle")
        Path(self.recorder.status(ROOM["id"])["file"]).write_bytes(b"sample-video-packet")
        for _ in range(100):
            if self.recorder.status(ROOM["id"])["state"] == "recording":
                break
            await asyncio.sleep(0.01)
        result = self.recorder.status(ROOM["id"])
        self.assertEqual(result["state"], "recording")
        self.assertIsNotNone(result["started_at"])

    async def test_failed_process_never_reports_recording(self):
        process = await self._start()
        process.finish(1)
        self.assertEqual(self.recorder.status(ROOM["id"])["state"], "error")
        self.assertNotIn("token", self.recorder.status(ROOM["id"])["error"])

    async def test_low_space_blocks_start(self):
        with patch.object(self.recorder, "_has_space", AsyncMock(return_value=False)):
            result = await self.recorder.sync(ROOM, LIVE)
        self.assertEqual(result["state"], "error")
        self.spawn.assert_not_awaited()

    async def test_low_space_stops_active_process(self):
        process = await self._start()
        with patch.object(self.recorder, "_has_space", AsyncMock(return_value=False)):
            for _ in range(100):
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(process.returncode, 0)
        result = self.recorder.status(ROOM["id"])
        self.assertEqual(result["state"], "error")
        self.assertIn("磁盘", result["error"])

    async def test_missing_ffmpeg_is_reported(self):
        with patch.object(recorder.shutil, "which", return_value=None):
            result = await self.recorder.sync(ROOM, LIVE)
        self.assertEqual(result["state"], "error")
        self.spawn.assert_not_awaited()

    async def test_room_path_traversal_is_rejected(self):
        result = await self.recorder.sync(ROOM | {"id": "../escape"}, LIVE)
        self.assertEqual(result["state"], "error")
        self.spawn.assert_not_awaited()

    async def test_delete_and_close_stop_recording(self):
        process = await self._start()
        await self.recorder.stop(ROOM["id"])
        self.assertEqual(process.returncode, 0)
        await self.recorder.sync(ROOM, LIVE)
        process = self.processes[-1]
        await self.recorder.close()
        self.assertEqual(process.returncode, 0)

    async def test_stop_invalidates_already_queued_live_snapshot(self):
        room_lock = self.recorder._lock(ROOM["id"])
        await room_lock.acquire()
        stale_sync = asyncio.create_task(self.recorder.sync(ROOM, LIVE))
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(self.recorder.stop(ROOM["id"]))
        await asyncio.sleep(0)
        room_lock.release()
        await asyncio.gather(stale_sync, stop_task)
        self.spawn.assert_not_awaited()

    async def test_stop_during_dns_prevents_process_start(self):
        entered_dns = asyncio.Event()
        release_dns = asyncio.Event()

        async def slow_dns(*args, **kwargs):
            entered_dns.set()
            await release_dns.wait()
            return PUBLIC_DNS

        with patch.object(asyncio.get_running_loop(), "getaddrinfo", slow_dns):
            start = asyncio.create_task(self.recorder.sync(ROOM, LIVE))
            await entered_dns.wait()
            stop = asyncio.create_task(self.recorder.stop(ROOM["id"]))
            await asyncio.sleep(0)
            release_dns.set()
            await asyncio.gather(start, stop)
        self.spawn.assert_not_awaited()

    async def test_cancellation_during_spawn_cleans_up_child(self):
        entered_spawn = asyncio.Event()
        release_spawn = asyncio.Event()
        process = _Process()

        async def slow_spawn(*args, **kwargs):
            entered_spawn.set()
            await release_spawn.wait()
            return process

        with patch.object(asyncio, "create_subprocess_exec", slow_spawn):
            start = asyncio.create_task(self.recorder.sync(ROOM, LIVE))
            await entered_spawn.wait()
            start.cancel()
            release_spawn.set()
            with self.assertRaises(asyncio.CancelledError):
                await start
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdin.written, [b"q\n"])
        self.assertIsNone(self.recorder._entries[ROOM["id"]].process)


if __name__ == "__main__":
    unittest.main()
