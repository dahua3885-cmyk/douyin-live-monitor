"""Local-only Douyin monitoring service. Run with Python 3.11+ and aiohttp."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import io
import json
import logging
import math
import faulthandler
import sys
from logging.handlers import RotatingFileHandler
from storage import validate_folder, transcript_filter, choose_folder, list_folders
from archive import Archive, migrate
from media import MediaService, SegmentRecorder
from notifications import Notifications
import os
from pathlib import Path
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
import uuid

from aiohttp import web

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = Path(os.environ.get("LIVE_MONITOR_DATA", str(ROOT / "data")))
LOG = logging.getLogger("live-monitor")
RECORDING_DEFAULTS = {'segment_minutes':0,'record_limit_minutes':0,'record_quality':'SD1','convert_mp4':True}
STATUSES = {"live", "offline", "unknown", "blocked", "error"}


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("请填写抖音直播间或主播主页链接")
    found = re.search(r"https?://[^\s<>\"'，。；！）)]+", value.strip())
    value = found.group(0) if found else value.strip()
    if value.startswith(("live.douyin.com/", "www.douyin.com/", "v.douyin.com/")):
        value = "https://" + value
    if re.fullmatch(r"\d{3,30}", value):
        value = "https://live.douyin.com/" + value
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError("请使用正常的抖音 HTTPS 链接")
    host = (p.hostname or "").lower()
    path = p.path.rstrip("/")
    if host in {"www.iesdouyin.com", "iesdouyin.com"} and path.startswith("/share/user/"):
        host, path = "www.douyin.com", path.replace("/share/user/", "/user/", 1)
    valid = (host == "live.douyin.com" and re.fullmatch(r"/\d{3,30}", path)) or (
        host in {"www.douyin.com", "douyin.com"} and re.fullmatch(r"/user/[A-Za-z0-9_-]{5,200}", path)
    ) or (host == "v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]{3,100}", path))
    if not valid:
        raise ValueError("支持 live.douyin.com 直播间、抖音主播主页和 v.douyin.com 分享链接")
    return urlunsplit(("https", "www.douyin.com" if host == "douyin.com" else host, path + ("/" if host == "v.douyin.com" else ""), "", ""))


def scrub_snapshot(snapshot):
    """Only persist the public normalized observation, never stream signing URLs."""
    keys = ("status", "nickname", "title", "room_id", "web_rid", "url", "online", "online_display", "likes", "total_viewers", "followers", "started_at", "source", "error", "observed_at", "approx_fields", "broadcast_id")
    value = {k: snapshot.get(k) for k in keys}
    value["status"] = value["status"] if value["status"] in STATUSES else "unknown"
    value["observed_at"] = value["observed_at"] or utcnow()
    value["approx_fields"] = value["approx_fields"] or []
    value["stream_available"] = bool(snapshot.get("stream_url"))
    for key in ("online", "likes", "total_viewers", "followers"):
        n = value[key]
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0:
            value[key] = None
    if value["source"] and str(value["source"]).startswith("http"):
        p = urlsplit(value["source"])
        value["source"] = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    return value


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "monitor.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms (
          id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
          enabled INTEGER NOT NULL DEFAULT 0, interval_seconds INTEGER NOT NULL DEFAULT 30,
          alert_above INTEGER, alert_below INTEGER, record_enabled INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'unknown', last_attempt TEXT, last_success TEXT,
          error TEXT, latest TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS observations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
          observed_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS observations_room_time ON observations(room_id, observed_at);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT, kind TEXT NOT NULL,
          message TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS speech_chunks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
          session_id TEXT NOT NULL, audio_path TEXT NOT NULL UNIQUE, start_seconds REAL NOT NULL,
          end_seconds REAL NOT NULL, captured_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          text TEXT NOT NULL DEFAULT '', segments TEXT, model TEXT, elapsed_seconds REAL,
          completed_at TEXT, error TEXT
        );
        CREATE INDEX IF NOT EXISTS speech_room_time ON speech_chunks(room_id,captured_at);
        """)
        if 'transcribe_enabled' not in {r[1] for r in self.db.execute('PRAGMA table_info(rooms)')}:
            self.db.execute('ALTER TABLE rooms ADD COLUMN transcribe_enabled INTEGER NOT NULL DEFAULT 0')
        migrate(self.db)
        if not self.setting('recording_preferences'):
            old=[dict(r) for r in self.db.execute('SELECT id,segment_minutes,record_limit_minutes,record_quality,convert_mp4 FROM rooms WHERE deleted=0 ORDER BY created_at,id')]
            variants=[{key:r[key] for key in RECORDING_DEFAULTS} for r in old]
            nondefault=[v for v in variants if v!=RECORDING_DEFAULTS]
            chosen=dict(nondefault[-1] if nondefault else variants[-1] if variants else RECORDING_DEFAULTS)
            chosen['convert_mp4']=bool(chosen['convert_mp4'])
            self.set_setting('recording_preferences_migration',json.dumps(old,ensure_ascii=False))
            self.set_setting('recording_preferences',json.dumps(chosen,ensure_ascii=False))
        self.db.execute("UPDATE speech_chunks SET status='pending' WHERE status='processing'")
        # Preserve a known account name when the platform's offline response omits it.
        for r in self.db.execute("SELECT id,latest FROM rooms WHERE name='' AND deleted=0").fetchall():
            latest=json.loads(r['latest'] or '{}')
            if not latest.get('nickname'):
                for old in self.db.execute("SELECT payload FROM observations WHERE room_id=? AND json_extract(payload,'$.nickname') IS NOT NULL ORDER BY id DESC LIMIT 1",(r['id'],)):
                    known=json.loads(old[0]).get('nickname')
                    if known:
                        latest['nickname']=known
                        self.db.execute('UPDATE rooms SET latest=? WHERE id=?',(json.dumps(latest,ensure_ascii=False),r['id']))
                        self.db.execute("UPDATE broadcasts SET name=? WHERE room_id=? AND name IN ('历史直播间','未命名直播间')",(known,r['id']))
                        break
        self.db.commit()

    def setting(self, key):
        row = self.db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        self.db.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    def recording_settings(self):
        return RECORDING_DEFAULTS | json.loads(self.setting('recording_preferences') or '{}')

    def save_recording_settings(self, changes):
        if not isinstance(changes,dict) or not changes or set(changes)-RECORDING_DEFAULTS.keys():
            raise ValueError('录像设置包含不支持的字段')
        for key,value in changes.items():
            if key in {'segment_minutes','record_limit_minutes'}:
                if type(value) is not int or not 0<=value<=720:raise ValueError('录像时长需为0—720分钟，0代表不限时。')
            elif key=='record_quality':
                if not isinstance(value,str) or value not in {'ORIGIN','FULL_HD1','HD1','SD1','SD2'}:raise ValueError('请选择有效画质')
            elif type(value) is not bool:raise ValueError('MP4 选项必须为开启或关闭')
        result=self.recording_settings()|changes
        self.set_setting('recording_preferences',json.dumps(result,ensure_ascii=False))
        return result

    def close(self):
        self.db.close()

    def rooms(self):
        return [self.decode(row) for row in self.db.execute("SELECT * FROM rooms WHERE deleted=0 ORDER BY created_at,id")]

    def decode(self,row):
        if row is None:
            return None
        r = dict(row)
        r["enabled"] = bool(r["enabled"])
        r["record_enabled"] = bool(r["record_enabled"])
        r["transcribe_enabled"] = bool(r["transcribe_enabled"])
        r["convert_mp4"] = bool(r["convert_mp4"])
        r["latest"] = json.loads(r["latest"]) if r["latest"] else None
        r.update(self.recording_settings())
        return r

    def get(self, rid):
        return self.decode(self.db.execute("SELECT * FROM rooms WHERE id=? AND deleted=0", (rid,)).fetchone())

    def add(self, url, name=""):
        url = normalize_url(url)
        existing = self.db.execute("SELECT id,deleted FROM rooms WHERE url=?", (url,)).fetchone()
        if existing:
            if existing['deleted']:
                self.db.execute("UPDATE rooms SET deleted=0,enabled=0,record_enabled=0,transcribe_enabled=0 WHERE id=?",(existing['id'],));self.db.commit()
                return self.get(existing['id'])
            raise ValueError("这个直播间已在监控列表中")
        if len(self.rooms()) >= 200:
            raise ValueError("这一版本最多保存 200 个直播间，建议同时监控不超过 5 个")
        rid = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO rooms(id,url,name,created_at) VALUES(?,?,?,?)", (rid, url, str(name or "").strip()[:100], utcnow()))
        self.db.commit()
        self.event(rid, "added", "已添加直播间，等待首次检查")
        return self.get(rid)

    def update(self, rid, changes, validate_only=False, commit=True):
        room = self.get(rid)
        if not room:
            raise KeyError(rid)
        if isinstance(changes,dict) and set(changes)&RECORDING_DEFAULTS.keys():
            raise ValueError("录像参数已改为全局统一设置，请刷新页面后在全局录像设置修改。")
        allowed = {"name", "enabled", "interval_seconds", "alert_above", "alert_below", "record_enabled", "transcribe_enabled", "group_name"}
        if not isinstance(changes, dict) or not changes or set(changes) - allowed:
            raise ValueError("设置中包含不支持的字段")
        values = {}
        for key, val in changes.items():
            if key in {"enabled", "record_enabled", "transcribe_enabled", "convert_mp4"}:
                if type(val) is not bool:
                    raise ValueError("监控和录制开关必须为开启或关闭")
                values[key] = int(val)
            elif key in {"segment_minutes", "record_limit_minutes"}:
                minimum=1 if key=='segment_minutes' else 0
                if type(val) is not int or not minimum<=val<=720:raise ValueError('分段需为1—720分钟；总时长需为0—720分钟，0代表不限时。')
                values[key]=val
            elif key=='record_quality':
                if val not in {'ORIGIN','FULL_HD1','HD1','SD1','SD2'}:raise ValueError('请选择有效画质')
                values[key]=val
            elif key == "group_name":
                values[key]=str(val or '').strip()[:50]
            elif key == "name":
                values[key] = str(val or "").strip()[:100]
            elif key == "interval_seconds":
                if type(val) is not int or not 15 <= val <= 300:
                    raise ValueError("检查间隔需为 15 至 300 秒的整数")
                values[key] = val
            else:
                if val is not None and (type(val) is not int or not 0 <= val <= 10**10):
                    raise ValueError("人数阈值需为非负整数，留空表示关闭")
                values[key] = val
        combined = room | values
        if combined["alert_above"] is not None and combined["alert_below"] is not None and combined["alert_below"] >= combined["alert_above"]:
            raise ValueError("低人数提醒阈值需要小于高人数提醒阈值")
        if validate_only:return combined
        sql = ",".join(f"{key}=?" for key in values)
        self.db.execute(f"UPDATE rooms SET {sql} WHERE id=?", (*values.values(), rid))
        if commit:self.db.commit()
        return self.get(rid)

    def delete(self, rid):
        self.db.execute("UPDATE rooms SET deleted=1,enabled=0,record_enabled=0,transcribe_enabled=0 WHERE id=?", (rid,))
        self.db.commit()

    def event(self, rid, kind, message):
        self.db.execute("INSERT INTO events(room_id,kind,message,created_at) VALUES(?,?,?,?)", (rid, kind, message, utcnow()))
        self.db.commit()

    def events(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")]

    def last_event(self, rid, kind):
        row = self.db.execute("SELECT created_at FROM events WHERE room_id=? AND kind=? ORDER BY id DESC LIMIT 1", (rid, kind)).fetchone()
        return row[0] if row else None

    def attempt(self, rid):
        self.db.execute("UPDATE rooms SET last_attempt=? WHERE id=?", (utcnow(), rid))
        self.db.commit()

    def observe(self, rid, snapshot):
        room = self.get(rid)
        if not room:
            return
        snap = scrub_snapshot(snapshot)
        for key in ("nickname","title","web_rid"):
            if not snap.get(key):snap[key]=(room.get("latest") or {}).get(key)
        payload = json.dumps(snap, ensure_ascii=False)
        self.db.execute("INSERT INTO observations(room_id,observed_at,status,payload,broadcast_id) VALUES(?,?,?,?,?)", (rid, snap["observed_at"], snap["status"], payload,snapshot.get("broadcast_id")))
        success = snap["status"] in {"live", "offline"}
        self.db.execute("UPDATE rooms SET status=?,error=?,latest=?,last_success=? WHERE id=?", (
            snap["status"], snap["error"], payload if success else json.dumps(room["latest"], ensure_ascii=False) if room["latest"] else None,
            snap["observed_at"] if success else room["last_success"], rid))
        self.db.commit()
        label = room["name"] or snap.get("nickname") or "直播间"
        previous = (room["latest"] or {}).get("status")
        if success and previous != snap["status"]:
            kind = "live" if snap["status"] == "live" else "offline"
            self.event(rid, kind, f"{label}：{'检测到直播中' if kind == 'live' else '确认当前未开播'}")
        elif not success and (room["status"] != snap["status"] or room["error"] != snap["error"]):
            self.event(rid, "collection_error", f"{label}：{snap['error'] or '暂未取得可确认的直播状态'}")
        if snap["status"] == "live" and snap["online"] is not None:
            approx = "约 " if "online" in snap["approx_fields"] else ""
            for kind, threshold, triggered in (
                ("above", room["alert_above"], room["alert_above"] is not None and snap["online"] > room["alert_above"]),
                ("below", room["alert_below"], room["alert_below"] is not None and snap["online"] < room["alert_below"]),
            ):
                last = self.last_event(rid, kind)
                cooled = not last or (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= 300
                if triggered and cooled:
                    self.event(rid, kind, f"{label}：在线 {approx}{snap['online']:,} 人，{'高于' if kind == 'above' else '低于'} {threshold:,} 人")

    def history(self, rid, hours=24):
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM observations WHERE room_id=? AND observed_at>=? ORDER BY observed_at,id", (rid, since))]


class Monitor:
    def __init__(self, store, collector, recorder, speech=None):
        self.store, self.collector, self.recorder = store, collector, recorder
        self.speech = speech
        self.forced = set()
        self.next_due = {}
        self.busy = None
        self.task = None
        self.wake = asyncio.Event()
        self.record_states = {}
        self.archive = Archive(store)

    def queue(self, rid):
        self.forced.add(rid)
        self.wake.set()

    async def run(self):
        while True:
            now = time.monotonic()
            rooms = self.store.rooms()
            due = [r for r in rooms if r["id"] in self.forced or (r["enabled"] and now >= self.next_due.get(r["id"], 0))]
            if not due:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            for room in due:
                rid = room["id"]
                forced = rid in self.forced
                self.forced.discard(rid)
                current = self.store.get(rid)
                if not current or (not current["enabled"] and not forced):
                    continue
                self.busy = rid
                self.store.attempt(rid)
                try:
                    target_url = current["url"]
                    resolved = (current.get("latest") or {}).get("url")
                    if urlsplit(target_url).hostname == "v.douyin.com" and resolved:
                        try:
                            target_url = normalize_url(resolved)
                        except ValueError:
                            pass
                    snapshot = await asyncio.wait_for(self.collector.collect(target_url), timeout=55)
                    if not isinstance(snapshot, dict):
                        raise ValueError("采集器返回格式异常")
                except asyncio.TimeoutError:
                    snapshot = {"status": "error", "error": "页面响应超时，将在下一轮重试", "observed_at": utcnow()}
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    LOG.warning("Collector failed: %s", type(exc).__name__)
                    snapshot = {"status": "error", "error": "采集暂时失败，请稍后重试或打开登录窗口", "observed_at": utcnow()}
                current = self.store.get(rid)
                if current:
                    broadcast_id=self.archive.on_observation(current,snapshot)
                    snapshot['broadcast_id']=broadcast_id
                    current['broadcast_id']=broadcast_id
                    self.store.observe(rid, snapshot)
                    try:
                        rec = await self.recorder.sync(current, snapshot)
                        state = rec.get("state")
                        if self.record_states.get(rid) != state:
                            self.record_states[rid] = state
                            if state == "recording" and not isinstance(self.recorder,SegmentRecorder):
                                self.store.event(rid, "recording", f"{current['name'] or '直播间'}：已开始录制")
                            elif state == "error" and not isinstance(self.recorder,SegmentRecorder):
                                self.store.event(rid, "record_error", rec.get("error") or "录制未能启动")
                    except Exception:
                        LOG.exception("Recorder failure")
                        self.store.event(rid, "record_error", "录制发生异常，监控数据继续采集")
                    if self.speech:
                        try:
                            # Re-read after any asynchronous recording startup; a
                            # pause/delete must also stop pending speech capture.
                            fresh = self.store.get(rid)
                            if fresh:
                                await self.speech.sync(fresh | {"broadcast_id":broadcast_id}, snapshot)
                        except Exception:
                            LOG.exception("Speech capture failure")
                    failures = snapshot.get("status") not in {"live", "offline"}
                    self.next_due[rid] = time.monotonic() + max(current["interval_seconds"], 60 if failures else 0)
                self.busy = None

    async def close(self):
        try:
            if self.task:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
        finally:
            try:
                if self.speech:
                    await self.speech.close()
            finally:
                try:
                    await self.recorder.close()
                finally:
                    await self.collector.close()


def stale(room):
    if room["status"] not in {"live", "offline"} or not room["last_success"]:
        return True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(room["last_success"])).total_seconds()
        return age > max(90, room["interval_seconds"] * 3)
    except (ValueError, TypeError):
        return True


@web.middleware
async def guard(request, handler):
    host = request.host
    if host not in {f"127.0.0.1:{request.app['port']}", f"localhost:{request.app['port']}"}:
        return web.json_response({"error": "仅允许从本机地址访问"}, status=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        if (origin and origin != f"http://{host}") or request.headers.get("Sec-Fetch-Site") == "cross-site":
            return web.json_response({"error": "不允许跨站修改本地监控设置"}, status=403)
        if request.method in {"POST", "PATCH"} and request.content_type != "application/json":
            return web.json_response({"error": "请求格式需为 JSON"}, status=415)
    try:
        response = await handler(request)
    except (ValueError, json.JSONDecodeError) as exc:
        response = web.json_response({"error": str(exc)}, status=400)
    except KeyError:
        response = web.json_response({"error": "直播间不存在"}, status=404)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    return response


def create_app(data_dir=DEFAULT_DATA, port=18765, collector=None, recorder=None, start_worker=True):
    if collector is None:
        from collector import DouyinCollector
        collector = DouyinCollector(Path(data_dir))
    store = Store(data_dir)
    media=MediaService(store)
    if recorder is None:
        recorder = SegmentRecorder(Path(data_dir),store,media)
        if store.setting("recording_dir"):
            recorder.recordings_dir = Path(store.setting("recording_dir"))
            recorder.folder_selected = True
    from speech import SpeechService
    speech = SpeechService(Path(data_dir), store)
    monitor = Monitor(store, collector, recorder, speech)
    app = web.Application(middlewares=[guard], client_max_size=256*1024)
    app["port"], app["store"], app["monitor"] = port, store, monitor
    app["shutdown_event"] = asyncio.Event()
    notifier=Notifications(store)
    archive=Archive(store)
    folder_lock = asyncio.Lock()
    picker_lock = asyncio.Lock()

    def get_room(request):
        room = store.get(request.match_info["rid"])
        if not room:
            raise KeyError("room")
        return room

    async def index(request):
        return web.FileResponse(ROOT / "static/index.html")

    async def health(request):
        healthy = all(task is None or not task.done() for task in (monitor.task, speech.task, media.task, notifier.task, archive.task))
        return web.json_response({"app": "dahua-live-monitor", "version": "0.4.3", "time": utcnow(), "pid": os.getpid(), "healthy": healthy}, status=200 if healthy else 503)

    async def shutdown(request):
        if request.query.get("pause") == "1":
            for room in store.rooms():
                store.update(room['id'], {'enabled': False})
        if request.query.get("restart") != "1":
            (store.data_dir / "supervisor.stop").touch()
        asyncio.get_running_loop().call_later(0.25, app["shutdown_event"].set)
        return web.json_response({"stopping": True})

    async def state(request):
        rooms = store.rooms()
        for room in rooms:
            room["stale"] = stale(room)
            room["record_status"] = recorder.status(room["id"])
            room["transcription_status"] = speech.status(room["id"])
        return web.json_response({"rooms": rooms, "events": store.events(), "collector": {"busy": monitor.busy},
            "settings": {"data_dir": str(store.data_dir), "recordings_dir": store.setting("recording_dir"), "autostart": (store.data_dir / "autostart.json").exists(), "recording": store.recording_settings()}, "server_time": utcnow()})

    async def add(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请输入直播间信息")
        room = store.add(body.get("url"), body.get("name", ""))
        monitor.queue(room["id"])
        return web.json_response({"room": room}, status=201)

    async def patch(request):
        room = get_room(request)
        body = await request.json()
        async with folder_lock:
            if isinstance(body, dict) and body.get("record_enabled") is True:
                folder = store.setting("recording_dir")
                await asyncio.to_thread(validate_folder, folder)
            updated = store.update(room["id"], body)
        if room['enabled'] and not updated['enabled']:
            active=archive.current(room['id'])
            if active:archive.gap(active['id'],utcnow(),None,'pause','用户暂停了监控，期间素材未采集')
        if not updated["enabled"]:
            monitor.forced.discard(room["id"])
        if not updated["enabled"] or not updated["record_enabled"]:
            await recorder.stop(room["id"])
        if not updated["enabled"] or not updated["transcribe_enabled"]:
            await speech.stop(room["id"])
        if updated["enabled"] and (not room["enabled"] or (updated["transcribe_enabled"] and not room["transcribe_enabled"]) or (updated["record_enabled"] and not room["record_enabled"])):
            monitor.queue(room["id"])
        monitor.wake.set()
        return web.json_response({"room": updated})

    async def delete(request):
        room = get_room(request)
        monitor.forced.discard(room["id"])
        store.update(room['id'],{'enabled':False,'record_enabled':False,'transcribe_enabled':False})
        active=archive.current(room['id'])
        if active:archive.gap(active['id'],utcnow(),None,'pause','账号已移出采集列表，历史档案保留')
        await recorder.stop(room["id"])
        await speech.stop(room["id"])
        speech.scan()
        store.delete(room["id"])
        return web.json_response({"deleted": True})

    async def refresh(request):
        room = get_room(request)
        monitor.queue(room["id"])
        return web.json_response({"queued": True})

    async def control(request):
        body = await request.json()
        if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
            raise ValueError("请选择开始或暂停")
        for room in store.rooms():
            store.update(room["id"], {"enabled": body["enabled"]})
            if body["enabled"]:
                monitor.queue(room["id"])
            else:
                monitor.forced.discard(room["id"])
                await recorder.stop(room["id"])
                await speech.stop(room["id"])
        return web.json_response({"enabled": body["enabled"]})

    async def login(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("请使用登录按钮打开独立浏览器")
        url = normalize_url(body["url"]) if body.get("url") else None
        # Keep the HTTP request responsive while the dedicated visible browser opens.
        try:
            result = await asyncio.wait_for(collector.open_login(url), timeout=45)
        except asyncio.TimeoutError:
            return web.json_response({"error": "正在等待本轮采集完成，请稍后再次打开登录窗口"}, status=409)
        return web.json_response(result or {"opened": True})

    async def history(request):
        room = get_room(request)
        hours = max(1, min(24*30, int(request.query.get("hours", "24"))))
        return web.json_response({"points": store.history(room["id"], hours), "sessions": []})

    async def recording_settings(request):
        if request.method=='GET':return web.json_response(store.recording_settings())
        async with folder_lock:
            settings=store.save_recording_settings(await request.json())
        return web.json_response(settings)

    async def recording_folder(request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('请选择文件夹')
        async with folder_lock:
            if any(r['record_enabled'] for r in store.rooms()):
                raise ValueError('请先关闭所有账号的视频录制，再更改保存文件夹。已有视频保留在原位置。')
            folder = await asyncio.to_thread(validate_folder, body.get('path'))
            store.set_setting('recording_dir', folder)
            recorder.recordings_dir = Path(folder)
            recorder.folder_selected = True
        return web.json_response({'path': folder})

    async def pick_folder(request):
        if picker_lock.locked():
            raise ValueError('文件夹选择窗口已打开，请先完成选择。')
        async with picker_lock:
            return web.json_response({'path': await choose_folder()})

    async def browse_folders(request):
        try:
            result=await asyncio.wait_for(asyncio.to_thread(list_folders,request.query.get('path')),15)
        except asyncio.TimeoutError:
            raise ValueError('读取目录超时，请换一个磁盘或直接填写路径')
        return web.json_response(result)

    async def transcript_days(request):
        room = get_room(request)
        rows = store.db.execute("SELECT date(captured_at,'localtime') AS day, COUNT(*) AS chunks FROM speech_chunks WHERE room_id=? GROUP BY day ORDER BY day DESC", (room['id'],)).fetchall()
        return web.json_response({'days': [dict(r) for r in rows]})

    async def transcripts(request):
        room = get_room(request)
        where, params = transcript_filter(room['id'], request.query.get('day'))
        before = request.query.get('before')
        if before:
            where += ' AND id<?'
            params.append(int(before))
        rows = store.db.execute('SELECT id,session_id,start_seconds,end_seconds,captured_at,status,text,model,elapsed_seconds,completed_at,error FROM speech_chunks WHERE '+where+' ORDER BY id DESC LIMIT 201', params).fetchall()
        items = [dict(row) for row in reversed(rows[:200])]
        return web.json_response({'items': items, 'has_more': len(rows)>200, 'summary': speech.status(room['id'])})

    def transcript_text(room, day=None):
        where, params = transcript_filter(room['id'], day)
        rows = store.db.execute('SELECT captured_at,status,text,error FROM speech_chunks WHERE '+where+' ORDER BY id', params).fetchall()
        lines = [f"{room['name'] or (room.get('latest') or {}).get('nickname') or '直播间'}｜自动话术转写草稿", '时间为本机采音时间；自动识别，数字和专有名词请复核。', '范围：'+(day or '全部已保存记录'), '']
        for row in rows:
            stamp = datetime.fromisoformat(row['captured_at']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            words = row['text'] if row['status']=='done' else {'silent':'[该段未检测到人声]','pending':'[等待转写]','processing':'[正在转写]','error':'[该段转写失败，未补写文本]'}.get(row['status'],'[未完成]')
            lines.append(f'[{stamp}] {words}')
        return '\ufeff'+'\n\n'.join(lines)+'\n'

    async def transcript_export(request):
        room = get_room(request)
        return web.Response(body=transcript_text(room, request.query.get('day')).encode('utf-8'),content_type='text/plain',headers={'Content-Disposition':f'attachment; filename="transcript-{room["id"]}.txt"'})

    async def transcript_save(request):
        room = get_room(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError('请选择导出日期')
        content = transcript_text(room, body.get('day'))
        folder = store.data_dir / 'exports'
        folder.mkdir(exist_ok=True)
        name = f"话术_{room['id']}_{datetime.now():%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}.txt"
        target = folder / name
        await asyncio.to_thread(target.write_text, content, encoding='utf-8')
        return web.json_response({'path': str(target), 'filename': name})

    async def transcript_retry(request):
        room = get_room(request)
        store.db.execute("UPDATE speech_chunks SET status='pending',error=NULL WHERE room_id=? AND status='error'",(room['id'],))
        store.db.commit()
        speech.last_errors.pop(room['id'],None)
        return web.json_response({"queued":True})

    async def export(request):
        room = get_room(request)
        points = store.history(room["id"], 24*3650)
        buf = io.StringIO(newline="")
        writer = csv.writer(buf)
        writer.writerow(["采集时间UTC", "状态", "在线人数", "点赞量", "累计观看", "粉丝数", "近似字段", "数据来源", "错误"])
        def safe(value):
            if value is None:
                return ""
            text = str(value)
            return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
        for p in points:
            writer.writerow([safe(p.get(k)) for k in ("observed_at", "status", "online", "likes", "total_viewers", "followers")] + [safe(",".join(p.get("approx_fields") or [])), safe(p.get("source")), safe(p.get("error"))])
        return web.Response(body=("\ufeff"+buf.getvalue()).encode("utf-8"), content_type="text/csv", headers={"Content-Disposition": f'attachment; filename="live-{room["id"]}.csv"'})

    async def lifecycle(application):
        if start_worker:
            media.scan()
            await media.recover_unclosed(include_open=True)
            media.task=asyncio.create_task(media.run())
            notifier.task=asyncio.create_task(notifier.run())
            archive.task=asyncio.create_task(archive.run())
            monitor.task = asyncio.create_task(monitor.run())
            speech.task = asyncio.create_task(speech.run())
        yield
        try:
            await monitor.close()
        finally:
            await archive.close()
            await media.close()
            await notifier.close()
            store.close()

    app.cleanup_ctx.append(lifecycle)
    from features import register_features
    register_features(app,store,archive,media,recorder,speech,monitor,notifier,folder_lock)
    app.router.add_get("/", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/recording-settings", recording_settings)
    app.router.add_post("/api/recording-settings", recording_settings)
    app.router.add_post("/api/recording-folder", recording_folder)
    app.router.add_post("/api/pick-folder", pick_folder)
    app.router.add_get("/api/folders", browse_folders)
    app.router.add_get("/api/rooms/{rid}/transcript-days", transcript_days)
    app.router.add_post("/api/rooms/{rid}/save-transcript", transcript_save)
    app.router.add_post("/api/rooms", add)
    app.router.add_patch("/api/rooms/{rid}", patch)
    app.router.add_delete("/api/rooms/{rid}", delete)
    app.router.add_post("/api/rooms/{rid}/refresh", refresh)
    app.router.add_get("/api/rooms/{rid}/history", history)
    app.router.add_get("/api/rooms/{rid}/export.csv", export)
    app.router.add_get("/api/rooms/{rid}/transcripts", transcripts)
    app.router.add_get("/api/rooms/{rid}/transcript.txt", transcript_export)
    app.router.add_post("/api/rooms/{rid}/transcripts/retry", transcript_retry)
    app.router.add_post("/api/control", control)
    app.router.add_post("/api/shutdown", shutdown)
    app.router.add_post("/api/login", login)
    app.router.add_static("/static/", ROOT / "static")
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地直播监控台")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    diagnostic = RotatingFileHandler(args.data_dir / "service.log", maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[diagnostic, logging.StreamHandler()])
    faulthandler.enable()
    async def serve():
        app = create_app(args.data_dir, args.port)
        def task_error(loop, context):
            LOG.error("Event loop failure: %s", context.get("message"), exc_info=context.get("exception"))
        asyncio.get_running_loop().set_exception_handler(task_error)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, host="127.0.0.1", port=args.port).start()
            LOG.info("Live monitor ready at http://127.0.0.1:%s", args.port)
            await app["shutdown_event"].wait()
        finally:
            LOG.info("Service shutdown: cleanup started")
            await runner.cleanup()
            LOG.info("Service shutdown: cleanup completed")
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
