"""Behavioral tests use synthetic observations in isolated temporary databases."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Store, normalize_url, scrub_snapshot, stale


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.room = self.store.add('https://live.douyin.com/123456789', '测试直播间')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_failed_observation_preserves_last_success_and_gaps(self):
        self.store.observe(self.room['id'], {'status': 'live', 'online': 0, 'room_id': 's1'})
        good = self.store.get(self.room['id'])
        self.store.observe(self.room['id'], {'status': 'blocked', 'error': '需要验证码'})
        failed = self.store.get(self.room['id'])
        self.assertEqual(failed['last_success'], good['last_success'])
        self.assertEqual(failed['latest']['online'], 0)
        self.assertTrue(stale(failed))
        points = self.store.history(self.room['id'])
        self.assertIsNone(points[-1]['online'])
        self.assertEqual(points[-1]['status'], 'blocked')

    def test_confirmation_after_error_does_not_duplicate_live_event(self):
        for status in ('live', 'error', 'live'):
            self.store.observe(self.room['id'], {'status': status, 'online': 20 if status=='live' else None})
        self.assertEqual(sum(e['kind']=='live' for e in self.store.events()), 1)

    def test_alerts_cool_down_and_do_not_use_stale_counts(self):
        self.store.update(self.room['id'], {'alert_above': 10})
        for _ in range(2):
            self.store.observe(self.room['id'], {'status': 'live', 'online': 11})
        self.store.observe(self.room['id'], {'status': 'error', 'online': 99})
        self.assertEqual(sum(e['kind']=='above' for e in self.store.events()), 1)

    def test_settings_survive_restart_and_duplicate_url_is_rejected(self):
        self.store.update(self.room['id'], {'interval_seconds': 60, 'alert_below': 5})
        self.store.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.store.get(self.room['id'])['interval_seconds'], 60)
        with self.assertRaises(ValueError):
            self.store.add(self.room['url'] + '?tracking=anything')

    def test_remove_room_keeps_archived_history(self):
        self.store.observe(self.room['id'], {'status': 'offline'})
        self.store.delete(self.room['id'])
        self.assertEqual(len(self.store.history(self.room['id'])), 1)

    def test_validation_does_not_corrupt_saved_config(self):
        for changes in ({'enabled': 'false'}, {'interval_seconds': 0}, {'alert_above': 3, 'alert_below': 5}):
            with self.assertRaises(ValueError):
                self.store.update(self.room['id'], changes)
        self.assertFalse(self.store.get(self.room['id'])['enabled'])


class InputTests(unittest.TestCase):
    def test_short_link_from_share_text(self):
        self.assertEqual(normalize_url('来和我一起支持Ta吧 https://v.douyin.com/DemoShortCode/ 复制打开'), 'https://v.douyin.com/DemoShortCode/')

    def test_reject_private_and_lookalike_urls(self):
        for value in ('http://127.0.0.1:9000/', 'https://live.douyin.com.evil.example/123', 'file:///etc/passwd', 'https://me@live.douyin.com/123', 'https://live.douyin.com:444/123', 'https://www.douyin.com/video/12345'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_url(value)

    def test_stream_credentials_not_persisted_and_missing_stays_none(self):
        snap = scrub_snapshot({'status':'live', 'stream_url':'https://cdn.example/secret', 'online':float('nan'), 'likes':True})
        self.assertNotIn('stream_url', snap)
        self.assertIsNone(snap['online'])
        self.assertIsNone(snap['likes'])
        self.assertIsNone(snap['total_viewers'])


if __name__ == '__main__':
    unittest.main()
