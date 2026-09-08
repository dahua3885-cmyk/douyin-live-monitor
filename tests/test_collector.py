"""Synthetic boundary fixtures; no recorded/live platform data is used here."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector import DouyinCollector, decode_page_json, extract_snapshot, normalize_url, parse_count

URL = "https://live.douyin.com/123456789"
API = "https://live.douyin.com/webcast/room/web/enter/"


def fixture_room(**changes):
    room = {"id_str": "900000000000000001", "web_rid": "123456789", "status": 2,
            "title": "合成测试直播间", "owner": {"nickname": "合成主播", "follow_info": {"follower_count": 800}},
            "user_count": 137, "like_count": 0, "stats": {"total_user": 2100}}
    room.update(changes)
    return {"data": {"data": [room]}}


class URLBoundaryTests(unittest.TestCase):
    def test_official_mobile_profile_share_and_short_trailing_slash(self):
        self.assertEqual(normalize_url('https://www.iesdouyin.com/share/user/MS4wSynthetic?from=share'),'https://www.douyin.com/user/MS4wSynthetic')
        self.assertEqual(normalize_url('https://v.douyin.com/ShortCode/'),'https://v.douyin.com/ShortCode/')
    def test_accept_share_text_and_remove_tracking(self):
        self.assertEqual(normalize_url("来看直播 https://live.douyin.com/123456789?from=share 复制打开"), URL)
        self.assertEqual(normalize_url("https://www.douyin.com/user/MS4wLjAB-test?from=share"), "https://www.douyin.com/user/MS4wLjAB-test")

    def test_reject_network_and_host_confusion(self):
        for value in ("http://live.douyin.com/123", "https://127.0.0.1/123", "https://localhost/123",
                      "https://live.douyin.com.evil.test/123", "https://live.douyin.com@evil.test/123",
                      "https://evil.test@live.douyin.com/123", "https://live.douyin.com:8080/123",
                      "https://live.douyin.com/%2e%2e/admin", "https://www.douyin.com/video/123",
                      "https://live.douyin.com/123\\@127.0.0.1", "https://live.douyin.com/123\nhttps://v.douyin.com/abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)

    def test_invalid_collect_never_starts_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            collector = DouyinCollector(Path(directory))
            result = asyncio.run(collector.collect("https://127.0.0.1/admin"))
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["online"])
            self.assertIsNone(collector._playwright)


class MeasurementBoundaryTests(unittest.TestCase):
    def profile_fixture(self, raw=None, **changes):
        nested = raw if raw is not None else {"status":2,"user_count":170,"owner":{"web_rid":"100001234567"},"stream_url":{"flv_pull_url":{"HD1":"http://pull-flv.example.douyincdn.com/stream.flv"}}}
        user={"nickname":"合成主页主播","sec_uid":"MS4wSynthetic","room_id":900000000000000002,"live_status":1,"follower_count":1000,"room_data":nested}
        user.update(changes)
        return {"user":user}

    def test_profile_embedded_room_inherits_parent_id_for_stream(self):
        payload=self.profile_fixture()
        for encoded in (False,True):
            nested=payload['user']['room_data']
            payload['user']['room_data']=json.dumps(nested) if encoded else nested
            result=extract_snapshot([('profile',payload)],'', 'https://www.douyin.com/user/MS4wSynthetic')
            self.assertEqual(result['room_id'],'900000000000000002')
            self.assertEqual(result['web_rid'],'100001234567')
            self.assertEqual(result['online'],170)
            self.assertEqual(result['followers'],1000)
            self.assertTrue(result['stream_url'])
            self.assertEqual(result['source'],'douyin-profile-room-data')

    def test_profile_cannot_take_another_users_room(self):
        payload=self.profile_fixture(sec_uid='MS4wOther')
        result=extract_snapshot([('profile',payload)],'', 'https://www.douyin.com/user/MS4wSynthetic')
        self.assertEqual(result['status'],'unknown')
        self.assertIsNone(result['stream_url'])

    def test_profile_rejects_conflicting_nested_room_id(self):
        payload=self.profile_fixture()
        payload['user']['room_data']['id_str']='999999999999999999'
        result=extract_snapshot([('profile',payload)],'', 'https://www.douyin.com/user/MS4wSynthetic')
        self.assertIsNone(result['stream_url'])
        self.assertEqual(result['source'],'douyin-profile-live_status')

    def test_offline_profile_does_not_revive_cached_stream(self):
        result=extract_snapshot([('profile',self.profile_fixture(live_status=0))],'', 'https://www.douyin.com/user/MS4wSynthetic')
        self.assertEqual(result['status'],'offline')
        self.assertIsNone(result['stream_url'])
        self.assertIsNone(result['online'])

    def test_malformed_profile_room_is_not_executed_or_assumed_ready(self):
        result=extract_snapshot([('profile',self.profile_fixture(raw='not-json'))],'', 'https://www.douyin.com/user/MS4wSynthetic')
        self.assertEqual(result['status'],'live')
        self.assertIsNone(result['stream_url'])

    def test_exact_zero_is_different_from_missing(self):
        result = extract_snapshot([(API, fixture_room(user_count=None))], "", URL)
        self.assertIsNone(result["online"])
        self.assertEqual(result["likes"], 0)
        self.assertEqual(result["total_viewers"], 2100)

    def test_cumulative_reach_must_not_be_online(self):
        result = extract_snapshot([], "直播中\n1.2万人看过\n热度 8800", URL)
        self.assertIsNone(result["online"])
        self.assertEqual(result["total_viewers"], 12000)
        self.assertIn("total_viewers", result["approx_fields"])
        self.assertEqual(result["status"], "unknown")

    def test_exact_online_wins_over_rounded_stats(self):
        result = extract_snapshot([(API, fixture_room(user_count=10324, stats={"user_count_str": "1.0万", "total_user": 88000}))], "", URL)
        self.assertEqual(result["online"], 10324)
        self.assertNotIn("online", result["approx_fields"])
        self.assertEqual(result["total_viewers"], 88000)

    def test_visible_audience_updates_replace_earlier_enter_snapshot(self):
        result = extract_snapshot([(API, fixture_room(user_count=50))], "3本场点赞\n在线观众\n·\n59", URL, ["在线观众 · 59"])
        self.assertEqual(result["online"], 59)
        self.assertEqual(result["likes"], 3)
        self.assertEqual(result["source"], "douyin-page-dom")

    def test_online_label_cannot_capture_an_unrelated_body_number(self):
        result = extract_snapshot([(API, fixture_room(user_count=50))], "在线观众\n·\n\n3本场点赞", URL)
        self.assertEqual(result["online"], 50)

    def test_enter_room_identity_excludes_other_data_entries(self):
        payload = fixture_room()
        payload["data"]["enter_room_id"] = "900000000000000002"
        result = extract_snapshot([(API + "?web_rid=123456789", payload)], "", URL)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["room_id"])

    def test_view_stats_requires_explicit_metric_label(self):
        payload = fixture_room(user_count=None, stats={}, room_view_stats={"display_value": 53, "display_long": "53在线观众"})
        result = extract_snapshot([(API, payload)], "", URL)
        self.assertEqual(result["online"], 53)
        self.assertIsNone(result["total_viewers"])

    def test_offline_discards_stale_online_without_inventing_zero(self):
        result = extract_snapshot([(API, fixture_room(status=4, user_count=300))], "", URL)
        self.assertEqual(result["status"], "offline")
        self.assertIsNone(result["online"])
        self.assertEqual(result["likes"], 0)

    def test_network_failure_or_gate_is_never_offline(self):
        for text, status in (("", "unknown"), ("请完成安全验证", "blocked"), ("请先登录后观看", "blocked")):
            result = extract_snapshot([], text, URL)
            self.assertEqual(result["status"], status)
            self.assertIsNone(result["online"])
        self.assertEqual(extract_snapshot([], "本场直播已结束", URL)["status"], "offline")

    def test_unsupported_status_is_not_assumed_offline(self):
        result = extract_snapshot([(API, fixture_room(status=1))], "", URL)
        self.assertEqual(result["status"], "unknown")

    def test_recommended_room_and_wrong_web_rid_cannot_replace_target(self):
        state = {"recommend_rooms": [fixture_room(web_rid="999")["data"]["data"][0]],
                 "roomStore": {"roomInfo": {"room": fixture_room()["data"]["data"][0]}}}
        result = extract_snapshot([("douyin-page-state", state)], "", URL)
        self.assertEqual(result["room_id"], "900000000000000001")
        self.assertEqual(result["online"], 137)
        wrong = extract_snapshot([(API, fixture_room(web_rid="999"))], "", URL)
        self.assertEqual(wrong["status"], "unknown")
        self.assertIsNone(wrong["online"])

    def test_live_room_banner_configuration_cannot_become_a_room(self):
        banner = {"data": {"top_right": {"id": 2764541, "status": 1, "title": "合成装饰条"}}}
        result = extract_snapshot([("https://live.douyin.com/webcast/room/in_room_banner/", banner)], "", URL)
        self.assertIsNone(result["room_id"])
        self.assertIsNone(result["title"])
        self.assertEqual(result["status"], "unknown")

    def test_metric_strings_reject_noncounts(self):
        for value in (True, -1, float("nan"), "热度 200", "--", "1人看过", None):
            self.assertIsNone(parse_count(value)[0])
        self.assertEqual(parse_count("1.2万+"), (12000, True))
        self.assertEqual(parse_count("0"), (0, False))

    def test_ambiguous_display_value_not_reach_or_online(self):
        payload = fixture_room(user_count=None, stats={}, room_view_stats={"display_value": "123456"})
        result = extract_snapshot([(API, payload)], "", URL)
        self.assertIsNone(result["online"])
        self.assertIsNone(result["total_viewers"])

    def test_profile_absent_room_id_does_not_prove_offline(self):
        profile_url = "https://www.douyin.com/user/MS4wSynthetic"
        payload = {"user": {"nickname": "合成主播", "sec_uid": "MS4wSynthetic", "room_id": 0}}
        self.assertEqual(extract_snapshot([("profile", payload)], "", profile_url)["status"], "unknown")
        payload["user"]["live_status"] = 0
        self.assertEqual(extract_snapshot([("profile", payload)], "", profile_url)["status"], "offline")

    def test_percent_and_flight_json_are_data_not_executed(self):
        obj = {"roomStore": {"roomInfo": {"room": fixture_room()["data"]["data"][0]}}}
        encoded = quote(json.dumps(obj))
        flight = "self.__next_f.push(" + json.dumps([1, "8:" + json.dumps(obj) + "\n"]) + ")"
        for script in (encoded, flight):
            payloads = [("douyin-page-state", value) for value in decode_page_json([script])]
            self.assertEqual(extract_snapshot(payloads, "", URL)["online"], 137)

    def test_untrusted_stream_hosts_not_exposed_to_recorder(self):
        for stream in ("https://127.0.0.1/private", "https://evil.test/a.flv", "http://pull.douyincdn.com:443/a.flv"):
            payload = fixture_room(stream_url={"flv_pull_url": {"HD1": stream}})
            self.assertIsNone(extract_snapshot([(API, payload)], "", URL)["stream_url"])

    def test_official_cdn_stream_protocol_is_preserved(self):
        for stream in ("http://pull.douyincdn.com/a.flv", "https://pull.douyincdn.com/a.flv"):
            payload = fixture_room(stream_url={"flv_pull_url": {"HD1": stream}})
            self.assertEqual(extract_snapshot([(API, payload)], "", URL)["stream_url"], stream)


if __name__ == "__main__":
    unittest.main()
