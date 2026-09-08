"""Conservative, independent Douyin live-page collector.

Only browser-visible platform responses / page data are read. No cookie import,
private API signing, CAPTCHA solver, or third-party executable is used. The
parser fixtures are synthetic; they do not establish production compatibility.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


EMPTY_FIELDS = ("nickname", "title", "room_id", "web_rid", "online", "online_display",
                "likes", "total_viewers", "followers", "started_at", "stream_url")
_LIVE_PATH = re.compile(r"/[A-Za-z0-9_-]{1,80}/?$")
_PROFILE_PATH = re.compile(r"/user/[A-Za-z0-9_.=-]{1,200}/?$")
_SHORT_PATH = re.compile(r"/[A-Za-z0-9_-]{1,100}/?$")
_NUMBER = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([万亿kKwWmM]?)\s*(\+?)$")
_SKIP_BRANCHES = {"recommend", "recommendations", "recommend_rooms", "recommendRooms",
                  "feed", "feed_data", "related", "related_rooms", "aweme_list", "awemeList"}
_ROOM_KEYS = {"room", "room_info", "roomInfo", "room_data", "roomData", "live_room", "liveRoom"}
_ROOM_RESPONSE_PATHS = {"/webcast/room/web/enter/", "/webcast/room/info/"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def empty_snapshot(url: str, error: str | None = None, status: str = "unknown") -> dict:
    return {**dict.fromkeys(EMPTY_FIELDS), "status": status, "url": url,
            "source": "douyin-browser", "error": error, "observed_at": utc_now(),
            "approx_fields": []}


def normalize_url(value: str) -> str:
    """Accept only HTTPS Douyin room/profile/short links, including pasted shares."""
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("请填写抖音直播间或主播主页链接。")
    value = value.strip()
    if not value.startswith("https://") or any(c.isspace() for c in value):
        matches = re.findall(r"https://[^\s<>\"'，。；！）】]+", value)
        if len(matches) != 1:
            raise ValueError("请只提供一个 https:// 开头的抖音链接。")
        value = matches[0]
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("链接包含不允许的字符。")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("链接格式不正确。") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        raise ValueError("仅支持标准 HTTPS 抖音链接。")
    path = parsed.path
    if host in ("www.iesdouyin.com", "iesdouyin.com") and path.startswith("/share/user/"):
        path = path.replace("/share/user/", "/user/", 1)
        host = "www.douyin.com"
    valid = ((host == "live.douyin.com" and bool(_LIVE_PATH.fullmatch(path))) or
             (host in ("www.douyin.com", "douyin.com") and bool(_PROFILE_PATH.fullmatch(path))) or
             (host == "v.douyin.com" and bool(_SHORT_PATH.fullmatch(path))))
    if not valid:
        raise ValueError("仅支持 live.douyin.com 直播间、抖音主播主页或 v.douyin.com 短链接。")
    # Share/tracking parameters are unnecessary, but short links may rely on them.
    query = parsed.query if host == "v.douyin.com" else ""
    return urlunsplit(("https", host, path.rstrip("/") + ("/" if host == "v.douyin.com" else ""), query, ""))


def parse_count(value: Any) -> tuple[int | None, bool]:
    """Return count + whether the website rounded/abbreviated the value."""
    if isinstance(value, bool) or value is None:
        return None, False
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0 or int(value) != value:
            return None, False
        return int(value), False
    if not isinstance(value, str):
        return None, False
    value = value.strip().replace(",", "").replace("，", "")
    match = _NUMBER.fullmatch(value)
    if not match:
        return None, False
    number, suffix, plus = match.groups()
    scale = {"万": 10_000, "亿": 100_000_000, "k": 1000, "w": 10_000, "m": 1_000_000}.get(suffix.lower(), 1)
    result = float(number) * scale
    if not math.isfinite(result) or result > 10**15:
        return None, False
    return int(result), bool(suffix or plus or "." in number)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first(obj: dict, *keys: str) -> Any:
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return None


def _id(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value).strip()
        if text and text != "0" and re.fullmatch(r"[A-Za-z0-9_.=-]{1,200}", text):
            return text
    return None


def _safe_stream(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    # The returned address is data only; collect() never downloads the stream.
    allowed = ("douyincdn.com", "douyinvod.com", "bytecdn.cn", "pstatp.com", "ibytedtos.com", "byteimg.com")
    protocol_ok = ((parsed.scheme == "https" and port in (None, 443)) or
                   (parsed.scheme == "http" and port in (None, 80)))
    if (protocol_ok and not parsed.username and
            not parsed.password and any(host == d or host.endswith("." + d) for d in allowed)):
        return value
    return None


def stream_choices(room: dict) -> dict:
    stream = _dict(_first(room, "stream_url", "streamUrl"))
    result = {}
    for key in ("flv_pull_url", "flvPullUrl", "hls_pull_url_map", "hlsPullUrlMap"):
        for quality, value in _dict(stream.get(key)).items():
            url = _safe_stream(value)
            if url and quality not in result:
                result[str(quality)] = url
    return result


def _stream(room: dict) -> str | None:
    stream = _dict(_first(room, "stream_url", "streamUrl"))
    for key in ("flv_pull_url", "flvPullUrl", "hls_pull_url_map", "hlsPullUrlMap"):
        choices = _dict(stream.get(key))
        for quality in ("FULL_HD1", "HD1", "SD1", "SD2", "ORIGIN"):
            result = _safe_stream(choices.get(quality))
            if result:
                return result
        for value in choices.values():
            result = _safe_stream(value)
            if result:
                return result
    return _safe_stream(_first(stream, "hls_pull_url", "hlsPullUrl"))


def _timestamp(value: Any) -> str | None:
    try:
        value = float(value)
        if value > 10**12:
            value /= 1000
        # A missing / bogus timestamp must never become a Unix-epoch start.
        if value < 1_500_000_000 or value > datetime.now(timezone.utc).timestamp() + 86400:
            return None
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _profile_room(profile: dict) -> dict | None:
    """A profile's room_data inherits its room identity from the parent user.

    The current website serializes room_data as JSON without its own room ID;
    generic room discovery cannot safely identify it without this parent bind.
    """
    raw = _first(profile, "room_data", "roomData")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return None
    if not isinstance(raw, dict):
        return None
    parent_id = _id(_first(profile, "room_id", "roomId"))
    nested_id = _id(_first(raw, "id_str", "id", "room_id", "roomId"))
    if parent_id and nested_id and parent_id != nested_id:
        return None
    if not (parent_id or nested_id):
        return None
    owner = _dict(raw.get("owner"))
    parent_sec = _first(profile, "sec_uid", "secUid")
    nested_sec = _first(owner, "sec_uid", "secUid")
    if parent_sec and nested_sec and parent_sec != nested_sec:
        return None
    trusted_owner = {k: profile[k] for k in ("uid", "sec_uid", "nickname", "follower_count", "follow_info") if k in profile}
    web_rid = _id(_first(owner, "web_rid", "webRid") or _first(raw, "web_rid", "webRid") or _first(profile, "web_rid", "webRid"))
    if web_rid and re.fullmatch(r"\d{3,30}", web_rid):
        trusted_owner["web_rid"] = web_rid
    room = dict(raw, id_str=parent_id or nested_id, owner=trusted_owner)
    if str(_first(profile, "live_status", "liveStatus")) == "0":
        room["status"] = 4  # Do not revive a closed broadcast from cached room_data.
    candidate = _room_candidate(room)
    candidate["source"] = "douyin-profile-room-data"
    return candidate


def _room_candidate(room: dict, owner_fallback: dict | None = None) -> dict:
    owner = _dict(_first(room, "owner", "anchor", "user")) or owner_fallback or {}
    stats = _dict(room.get("stats"))
    result = empty_snapshot("")
    result.update(nickname=_first(owner, "nickname", "nick_name", "nickName"),
                  title=room.get("title"), room_id=_id(_first(room, "id_str", "id", "room_id", "roomId")),
                  web_rid=_id(_first(room, "web_rid", "webRid") or _first(owner, "web_rid", "webRid")),
                  stream_url=_stream(room), stream_choices=stream_choices(room),
                  started_at=_timestamp(_first(room, "start_time", "startTime", "create_time", "createTime")))
    # status=2/4 is the web room schema, not the separate profile live_status.
    state = room.get("status")
    if str(state) == "2":
        result["status"] = "live"
    elif str(state) == "4":
        result["status"] = "offline"
    elif result["stream_url"]:
        result["status"] = "live"
    metrics = {
        "online": [_first(room, "user_count", "userCount"), _first(stats, "user_count", "userCount"),
                   _first(stats, "user_count_str", "userCountStr"), _first(room, "user_count_str", "userCountStr")],
        "likes": [_first(room, "like_count", "likeCount"), _first(stats, "like_count", "likeCount")],
        "total_viewers": [_first(stats, "total_user", "totalUser", "total_user_str", "totalUserStr"),
                          _first(room, "total_user", "totalUser")],
        "followers": [_first(_dict(_first(owner, "follow_info", "followInfo")), "follower_count", "followerCount"),
                      _first(owner, "follower_count", "followerCount")],
    }
    for field, candidates in metrics.items():
        for value in candidates:
            parsed, approximate = parse_count(value)
            if parsed is not None:
                result[field] = parsed
                if approximate:
                    result["approx_fields"].append(field)
                if field == "online":
                    result["online_display"] = str(value)
                break
    # display_value without its label is ambiguous (often cumulative reach).
    view_stats = _dict(_first(room, "room_view_stats", "roomViewStats"))
    label = str(_first(view_stats, "display_long", "displayLong", "display_str", "displayStr") or "")
    if result["total_viewers"] is None and ("看过" in label or "累计观看" in label):
        number, approx = parse_count(_first(view_stats, "display_value", "displayValue"))
        result["total_viewers"] = number
        if approx:
            result["approx_fields"].append("total_viewers")
    if result["online"] is None and ("在线观众" in label or "人在线" in label):
        number, approx = parse_count(_first(view_stats, "display_value", "displayValue"))
        result["online"] = number
        result["online_display"] = str(_first(view_stats, "display_short", "displayShort") or number) if number is not None else None
        if approx:
            result["approx_fields"].append("online")
    if result["status"] == "offline":
        # Historical room payloads often still contain their last online count.
        result["online"] = result["online_display"] = result["stream_url"] = None
        result["approx_fields"] = [f for f in result["approx_fields"] if f != "online"]
    return result


def decode_page_json(script_texts: list[str]) -> list[Any]:
    """Decode JSON state / percent encoding and Next flight string envelopes."""
    result: list[Any] = []
    decoder = json.JSONDecoder()
    for original in script_texts[:80]:
        if not isinstance(original, str) or len(original) > 4_000_000:
            continue
        text = unquote(original.strip()) if original.lstrip().startswith(("%7B", "%5B", "%7b", "%5b")) else original.strip()
        try:
            result.append(json.loads(text))
            continue
        except (ValueError, RecursionError):
            pass
        # Decode JSON literals as data. Never eval or execute captured script.
        for match in list(re.finditer(r"(?:push\(|=)\s*([\[{])", text))[:30]:
            try:
                obj, _ = decoder.raw_decode(text[match.start(1):])
                if isinstance(obj, list) and len(obj) == 2 and isinstance(obj[1], str):
                    flight = obj[1]
                    for start in list(re.finditer(r"[\[{]", flight))[:100]:
                        try:
                            inner, _ = decoder.raw_decode(flight[start.start():])
                            if isinstance(inner, (dict, list)):
                                result.append(inner)
                                break
                        except (ValueError, RecursionError):
                            continue
                else:
                    result.append(obj)
            except (ValueError, RecursionError):
                continue
    return result


def _collect_candidates(payload: Any, source: str) -> tuple[list[dict], list[dict]]:
    rooms: list[dict] = []
    profiles: list[dict] = []
    source_path = urlsplit(source).path
    room_response = source_path in _ROOM_RESPONSE_PATHS
    if source.startswith("https://") and not room_response and "/user/profile/other/" not in source_path:
        return rooms, profiles
    if room_response:
        envelope = _dict(_dict(payload).get("data"))
        raw_rooms = envelope.get("data")
        if not isinstance(raw_rooms, list):
            raw_rooms = [envelope.get("room") or envelope.get("room_info") or envelope]
        bound_id = _id(envelope.get("enter_room_id"))
        requested_rid = _id(parse_qs(urlsplit(source).query).get("web_rid", [None])[0])
        for room in raw_rooms[:10]:
            room = _dict(room)
            room_id = _id(_first(room, "id_str", "id", "room_id", "roomId"))
            if not room_id or (bound_id and room_id != bound_id):
                continue
            if "status" not in room or not any(k in room for k in ("owner", "stream_url", "streamUrl")):
                continue
            candidate = _room_candidate(room)
            if candidate["web_rid"] and requested_rid and candidate["web_rid"] != requested_rid:
                continue
            candidate["web_rid"] = candidate["web_rid"] or requested_rid
            candidate["source"] = urlunsplit(("https", urlsplit(source).netloc, source_path, "", ""))
            if candidate["stream_url"] is None and candidate["status"] != "offline":
                candidate["stream_url"] = _stream({"stream_url": envelope.get("web_stream_url")})
                candidate["stream_choices"] = stream_choices({"stream_url": envelope.get("web_stream_url")})
            rooms.append(candidate)
        return rooms, profiles
    seen: set[int] = set()
    stack = [(payload, "", 0)]
    visited = 0
    while stack and visited < 5000:
        obj, parent, depth = stack.pop()
        visited += 1
        if depth > 18 or id(obj) in seen:
            continue
        if isinstance(obj, (dict, list)):
            seen.add(id(obj))
        if isinstance(obj, list):
            stack.extend((v, parent, depth + 1) for v in reversed(obj[:100]))
            continue
        if not isinstance(obj, dict):
            continue
        room_id = _id(_first(obj, "id_str", "id", "room_id", "roomId"))
        has_room_structure = ("status" in obj and ("owner" in obj or "title" in obj or "stream_url" in obj or "streamUrl" in obj))
        if room_id and has_room_structure and (parent in _ROOM_KEYS or (room_response and parent == "data") or "roomStore" in source):
            candidate = _room_candidate(obj)
            candidate["source"] = source
            if candidate["stream_url"] is None and room_response:
                # Some web versions supply playback URLs beside data.data[0].
                outer_stream = _dict(_dict(payload).get("data")).get("web_stream_url")
                candidate["stream_url"] = _stream({"stream_url": outer_stream})
                candidate["stream_choices"] = stream_choices({"stream_url": outer_stream})
                if candidate["status"] == "offline":
                    candidate["stream_url"] = None
            rooms.append(candidate)
            # Do not accidentally treat nested owner data as another room.
            continue
        if parent in {"user", "userInfo", "user_info"} and ("nickname" in obj and
                any(k in obj for k in ("sec_uid", "secUid", "uid", "unique_id", "live_status", "liveStatus"))):
            profiles.append(obj)
        for key, value in reversed(list(obj.items())):
            if key in _SKIP_BRANCHES or "recommend" in key.lower():
                continue
            if isinstance(value, (dict, list)):
                stack.append((value, key, depth + 1))
            elif key in ("room_data", "roomData") and isinstance(value, str):
                try:
                    stack.append((json.loads(value), key, depth + 1))
                except ValueError:
                    pass
    return rooms, profiles


def extract_snapshot(payloads: list[tuple[str, Any]], text: str, url: str, dom_metrics: list[str] | None = None) -> dict:
    """Pure parser; source must identify browser response URL or page-state origin."""
    result = empty_snapshot(url)
    host = urlsplit(url).hostname
    expected_rid = urlsplit(url).path.strip("/") if host == "live.douyin.com" else None
    rooms: list[dict] = []
    profiles: list[dict] = []
    for source, payload in payloads:
        candidates, users = _collect_candidates(payload, source)
        rooms.extend(candidates)
        profiles.extend(users)
    if host != "live.douyin.com" and "/user/" in urlsplit(url).path:
        expected_sec = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        profiles = [p for p in profiles if not _first(p, "sec_uid", "secUid") or _first(p, "sec_uid", "secUid") == expected_sec]
        # On a user page accept only the room bound to that user, not other
        # room-shaped objects or recommendation payloads in the same document.
        rooms = []
        for profile in profiles:
            candidate = _profile_room(profile)
            if candidate:
                rooms.append(candidate)
    matching = [room for room in rooms if not expected_rid or not room["web_rid"] or room["web_rid"] == expected_rid]
    if matching:
        # Prefer a known matching room and API data; never merge metrics from
        # several room IDs, including a recommended room embedded on the page.
        def score(room: dict) -> tuple:
            return (bool(expected_rid and room["web_rid"] == expected_rid),
                    "/webcast/room/" in room["source"] or room["source"] == "douyin-profile-room-data", room["status"] != "unknown",
                    sum(room.get(f) is not None for f in EMPTY_FIELDS))
        best = max(enumerate(matching), key=lambda pair: (score(pair[1]), pair[0]))[1]
        result.update(best, url=url, observed_at=utc_now())
        if expected_rid:
            result["web_rid"] = expected_rid
    if profiles:
        # The user profile endpoint is an explicit single-user source.
        profile = profiles[0]
        if result["nickname"] is None:
            result["nickname"] = profile.get("nickname")
        if result["followers"] is None:
            result["followers"], approx = parse_count(_first(profile, "follower_count", "followerCount"))
            if approx:
                result["approx_fields"].append("followers")
        if host != "live.douyin.com" and result["status"] == "unknown":
            live_status = _first(profile, "live_status", "liveStatus")
            if str(live_status) == "0":
                result["status"] = "offline"
                result["source"] = "douyin-profile-live_status"
            elif str(live_status) == "1":
                result["status"] = "live"
                result["source"] = "douyin-profile-live_status"
            result["web_rid"] = _id(_first(profile, "web_rid", "webRid"))
            result["room_id"] = _id(_first(profile, "room_id", "roomId"))
    # Text outside the current room can describe recommendations. Use it only
    # as a fallback on a room page, with labels that explicitly name the metric.
    if host == "live.douyin.com":
        patterns = {
            "online": [r"(?:在线观众|当前在线|在线人数|正在观看)\s*[·:：]?\s*([\d,.]+\s*[万亿wWkKmM]?\+?)",
                       r"([\d,.]+\s*[万亿wWkKmM]?\+?)\s*人(?:在线|正在观看)"],
            "total_viewers": [r"([\d,.]+\s*[万亿wWkKmM]?\+?)\s*人看过", r"累计观看\s*[:：]?\s*([\d,.]+\s*[万亿wWkKmM]?\+?)"],
        }
        for field, expressions in patterns.items():
            if (field != "online" and result[field] is not None) or (field == "online" and result["status"] == "offline"):
                continue
            for expression in expressions:
                # Online counts must come from one compact, visible DOM node;
                # never scan body text across unrelated labels or blank lines.
                metric_text = "\n".join(dom_metrics or []) if field == "online" else text[:60_000]
                match = re.search(expression, metric_text)
                if match:
                    result[field], approx = parse_count(match.group(1))
                    result["approx_fields"] = [f for f in result["approx_fields"] if f != field]
                    if approx:
                        result["approx_fields"].append(field)
                    if field == "online":
                        result["online_display"] = match.group(1).strip()
                        # Visible DOM is read after the initial room-enter API
                        # and includes the page's newer live audience updates.
                        result["source"] = "douyin-page-dom"
                    break
        likes_match = re.search(r"([\d,.]+\s*[万亿wWkKmM]?\+?)\s*本场点赞", text[:60_000])
        if likes_match:
            result["likes"], approx = parse_count(likes_match.group(1))
            result["approx_fields"] = [f for f in result["approx_fields"] if f != "likes"]
            if approx:
                result["approx_fields"].append("likes")
        if result["status"] == "unknown" and re.search(r"(?:本场直播已结束|直播已结束|主播暂未开播|主播暂未开播|主播未开播)", text):
            result["status"] = "offline"
            result["online"] = result["online_display"] = None
            result["source"] = "douyin-page-status-text"
    if result["status"] == "unknown":
        if re.search(r"请完成.{0,12}验证|拖动滑块|安全验证|验证码|验证后继续|访问过于频繁", text):
            result["status"] = "blocked"
            result["error"] = "抖音页面要求安全验证，请点击“打开抖音登录”在独立浏览器中人工完成。"
        elif re.search(r"登录后(?:继续|观看|查看)|请先登录|扫码登录后", text):
            result["status"] = "blocked"
            result["error"] = "抖音要求登录后访问，请点击“打开抖音登录”完成登录后重试。"
        else:
            result["error"] = "页面没有提供可确认的直播状态；可能仍在加载或当前页面结构不受支持。"
    result["approx_fields"] = sorted(set(result["approx_fields"]))
    return result


class DouyinCollector:
    """One collection at a time with an independent, persistent browser profile."""
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.profile_dir = self.data_dir / "browser-profile"
        self._lock = asyncio.Lock()
        self._playwright = None
        self._context = None
        self._headless = True
        self._login_page = None

    async def _ensure_context(self, headless: bool | None = None):
        target_headless = self._headless if headless is None else headless
        if self._context is not None and target_headless != self._headless:
            await self._context.close()
            self._context = None
        if self._context is not None:
            try:
                # Access to pages itself does not touch any user's other browser.
                if self._context.browser and self._context.browser.is_connected():
                    return self._context
            except Exception:
                pass
            self._context = None
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        last_error = None
        portable_browser = os.environ.get("LIVE_MONITOR_BROWSER_EXE")
        for channel in ((None,) if portable_browser else ("msedge", "chrome", None)):
            options = {"user_data_dir": str(self.profile_dir), "headless": target_headless,
                       "viewport": {"width": 1365, "height": 900}, "locale": "zh-CN",
                       "accept_downloads": False, "timeout": 12_000}
            if channel:
                options["channel"] = channel
            if portable_browser:
                options["executable_path"] = portable_browser
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(**options)
                self._headless = target_headless
                self._context.set_default_timeout(5000)
                return self._context
            except Exception as exc:
                last_error = exc
        raise RuntimeError("无法启动独立采集浏览器。请确认已安装 Microsoft Edge 或 Chrome，且未同时运行两个监控台。") from last_error

    @staticmethod
    def _navigation_allowed(url: str, login: bool = False) -> bool:
        if url == "about:blank":
            return True
        try:
            if login and url in ("https://live.douyin.com/", "https://live.douyin.com", "https://www.douyin.com/"):
                return True
            normalize_url(url)
            return True
        except ValueError:
            return False

    async def _guard_route(self, route, login: bool = False):
        request = route.request
        parsed = urlsplit(request.url)
        host = (parsed.hostname or "").lower()
        private = host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost", ".internal"))
        try:
            private = private or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        main_navigation = request.is_navigation_request() and request.frame == request.frame.page.main_frame
        if private or (main_navigation and not self._navigation_allowed(request.url, login=login)):
            await route.abort()
        elif not login and request.resource_type in {"media", "font"}:
            # Monitoring does not need to download video or audio.
            await route.abort()
        else:
            await route.continue_()

    async def _read_page(self, page, url: str) -> tuple[dict, str | None]:
        payloads: list[tuple[str, Any]] = []
        response_tasks: set[asyncio.Task] = set()
        http_blocked: list[int] = []
        primary_response = asyncio.Event()

        async def handle_response(response):
            try:
                parsed = urlsplit(response.url)
                host = (parsed.hostname or "").lower()
                if not (host == "douyin.com" or host.endswith(".douyin.com")):
                    return
                if parsed.path not in _ROOM_RESPONSE_PATHS and "/user/profile/other/" not in parsed.path:
                    return
                if response.status in (401, 403, 429):
                    http_blocked.append(response.status)
                    return
                if response.status != 200:
                    return
                response_rid = parse_qs(parsed.query).get("web_rid", [None])[0]
                current = urlsplit(page.url)
                if "/user/profile/other/" in parsed.path and "/user/" in current.path:
                    expected_sec = current.path.rstrip("/").rsplit("/", 1)[-1]
                    response_sec = parse_qs(parsed.query).get("sec_user_id", [None])[0]
                    if response_sec and response_sec != expected_sec:
                        return
                expected_rid = current.path.strip("/") if current.hostname == "live.douyin.com" else None
                if response_rid and expected_rid and response_rid != expected_rid:
                    return
                body = await response.body()
                if len(body) <= 4_000_000:
                    # Keep only the public room identity from the response URL;
                    # signatures and all other query values are discarded.
                    identity_query = "web_rid=" + response_rid if response_rid and _id(response_rid) else ""
                    payloads.append((urlunsplit((parsed.scheme, parsed.netloc, parsed.path, identity_query, "")), json.loads(body)))
                    primary_response.set()
            except Exception:
                return

        def on_response(response):
            task = asyncio.create_task(handle_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", on_response)
        try:
            nav_error = None
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=18_000)
                if response and response.status in (401, 403, 429):
                    http_blocked.append(response.status)
            except Exception as exc:
                nav_error = type(exc).__name__
            if urlsplit(page.url).hostname in {"www.iesdouyin.com", "iesdouyin.com"}:
                # Official short shares can land on the mobile user share page.
                # Move the same sec_uid to its desktop profile, not a guessed room.
                canonical = normalize_url(page.url)
                await page.goto(canonical, wait_until="domcontentloaded", timeout=12_000)
            if not self._navigation_allowed(page.url):
                return empty_snapshot(url, "链接跳转到了不支持的地址；请直接提供 live.douyin.com 直播间地址。", "error"), None
            # Bounded settling allows ordinary page requests to complete. This is
            # not an attempt to bypass challenges or continuously retry them.
            await page.wait_for_timeout(2200)
            if not primary_response.is_set():
                try:
                    await asyncio.wait_for(primary_response.wait(), timeout=4)
                except TimeoutError:
                    pass
            scripts = await page.locator("script").evaluate_all("els => els.map(e => e.textContent || '').filter(t => /roomStore|roomInfo|room_info|room_data|live_status|userInfo|RENDER_DATA/.test(t) || t.startsWith('%7B')).slice(0, 80)")
            for state in decode_page_json(scripts):
                payloads.append(("douyin-page-state", state))
            visible = await page.evaluate(r"""() => ({
                text: document.body?.innerText || '',
                metrics: [...document.querySelectorAll('span, div, p')].map(el => {
                    if (!el.getClientRects().length) return '';
                    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
                    return t.length <= 50 ? t : '';
                }).filter(t => /^(?:在线观众|当前在线|在线人数|正在观看)\s*[·:：]?\s*[\d,.]+\s*[万亿wWkKmM]?\+?$/.test(t)
                    || /^[\d,.]+\s*[万亿wWkKmM]?\+?\s*人(?:在线|正在观看)$/.test(t)).slice(0, 30)
            })""")
            text = visible["text"]
            if response_tasks:
                await asyncio.wait(response_tasks, timeout=1.5)
            result = extract_snapshot(payloads, text, page.url, visible["metrics"])
            if result["status"] == "unknown" and http_blocked:
                result.update(status="blocked", error=f"抖音返回访问限制（HTTP {http_blocked[-1]}），请人工登录或稍后重试。")
            elif result["status"] == "unknown" and nav_error:
                result.update(error="直播页面加载未完成或网络连接失败，请稍后重试。")
            next_url = None
            if urlsplit(page.url).hostname != "live.douyin.com" and result.get("web_rid"):
                try:
                    next_url = normalize_url("https://live.douyin.com/" + result["web_rid"])
                except ValueError:
                    pass
            if urlsplit(page.url).hostname != "live.douyin.com" and not next_url and result["status"] != "offline":
                # These selectors scope live links to the profile's own avatar.
                links = await page.locator('[data-e2e="user-live-avatar"] a[href], a[data-e2e="user-live-avatar"][href], [data-e2e="user-info"] a[href*="live.douyin.com"]').evaluate_all("els => els.map(e => e.href)")
                for link in links[:5]:
                    try:
                        candidate = normalize_url(link)
                        if urlsplit(candidate).hostname == "live.douyin.com":
                            next_url = candidate
                            break
                    except ValueError:
                        pass
            return result, next_url
        finally:
            page.remove_listener("response", on_response)
            pending = list(response_tasks)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def collect(self, url: str) -> dict:
        try:
            url = normalize_url(url)
        except ValueError as exc:
            return empty_snapshot(str(url)[:300], str(exc), "error")
        page = None
        try:
            async with asyncio.timeout(32):
                async with self._lock:
                    context = await self._ensure_context()
                    page = await context.new_page()
                    await page.route("**/*", self._guard_route)
                    result, next_url = await self._read_page(page, url)
                    if next_url and result["status"] != "offline":
                        profile_result = result
                        try:
                            live_result, _ = await asyncio.wait_for(self._read_page(page, next_url), timeout=12)
                        except Exception:
                            if profile_result.get("stream_url"):
                                return profile_result
                            raise
                        same_room = not (profile_result.get("room_id") and live_result.get("room_id")) or profile_result["room_id"] == live_result["room_id"]
                        if live_result["status"] in {"live", "offline"} and same_room:
                            if live_result.get("followers") is None:
                                live_result["followers"] = profile_result.get("followers")
                            if live_result["status"] == "live":
                                for field in ("stream_url", "stream_choices", "online", "online_display"):
                                    if live_result.get(field) is None:
                                        live_result[field] = profile_result.get(field)
                            result = live_result
                        elif profile_result.get("stream_url"):
                            # The profile supplied a real room-scoped stream;
                            # a slower live-page load must not discard it.
                            result = profile_result
                        else:
                            result = live_result
                    return result
        except TimeoutError:
            return empty_snapshot(url, "本轮采集超过 32 秒。可能页面加载缓慢、需要登录，或浏览器正忙；未确认下播。", "error")
        except ImportError:
            return empty_snapshot(url, "采集依赖 Playwright 尚未安装，请运行本项目安装脚本。", "error")
        except RuntimeError as exc:
            return empty_snapshot(url, str(exc)[:300], "error")
        except Exception:
            return empty_snapshot(url, "采集浏览器访问失败，请重试；若要求登录或验证，请打开抖音登录。", "error")
        finally:
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=3)
                except Exception:
                    pass

    async def open_login(self, url: str | None = None) -> dict:
        """User-triggered only: open a visible independent browser for manual login."""
        target = normalize_url(url) if url else "https://live.douyin.com/"
        async with self._lock:
            context = await self._ensure_context(headless=False)
            page = self._login_page
            if page is None or page.is_closed():
                page = await context.new_page()
                await page.route("**/*", lambda route: self._guard_route(route, login=True))
                self._login_page = page
            try:
                await page.goto(target, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass
            await page.bring_to_front()
            return {"ok": True, "status": "opened", "message": "已打开独立抖音浏览器，请在该窗口人工登录或验证，完成后回到监控台重试。"}

    async def close(self):
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                finally:
                    self._context = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                finally:
                    self._playwright = None
