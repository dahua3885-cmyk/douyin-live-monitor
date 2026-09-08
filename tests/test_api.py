import asyncio
import tempfile
from pathlib import Path
import sys
import unittest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_app


class FakeCollector:
    def __init__(self):
        self.calls = 0
        self.closed = False
    async def collect(self, url):
        self.calls += 1
        return {'status': 'live', 'online': 12, 'nickname': '合成测试', 'source': 'synthetic-fixture'}
    async def close(self):
        self.closed = True
    async def open_login(self, url=None):
        return {'opened': True}


class FakeRecorder:
    def __init__(self):
        self.stopped = []
    async def sync(self, room, snapshot):
        return self.status(room['id'])
    def status(self, rid):
        return {'state': 'idle', 'file': None, 'error': None}
    async def stop(self, rid):
        self.stopped.append(rid)
    async def close(self):
        pass


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.collector, self.recorder = FakeCollector(), FakeRecorder()
        self.app = create_app(self.tmp.name, collector=self.collector, recorder=self.recorder, start_worker=False)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()
        self.app['port'] = self.server.port

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_add_validate_patch_export_pause_and_delete(self):
        result = await self.client.post('/api/rooms', json={'url':'https://live.douyin.com/123456789'})
        self.assertEqual(result.status, 201)
        room = (await result.json())['room']
        rid = room['id']
        self.assertFalse(room['record_enabled'])
        self.assertEqual((await self.client.patch(f'/api/rooms/{rid}', json={'enabled': True})).status, 200)
        self.app['store'].observe(rid, {'status':'live','online':0, 'title':'=untrusted'})
        self.app['store'].observe(rid, {'status':'error','error':'=test'})
        exported = await self.client.get(f'/api/rooms/{rid}/export.csv')
        text = await exported.text()
        self.assertIn("'=test", text)
        self.assertIn(',live,0,', text)
        await self.client.post('/api/control', json={'enabled':False})
        self.assertIn(rid, self.recorder.stopped)
        self.assertNotIn(rid, self.app['monitor'].forced)
        state = await (await self.client.get('/api/state')).json()
        self.assertTrue(state['rooms'][0]['stale'])
        await self.client.delete(f'/api/rooms/{rid}')
        self.assertEqual((await self.client.get(f'/api/rooms/{rid}/history')).status, 404)

    async def test_cross_origin_cannot_start_local_monitor(self):
        response = await self.client.post('/api/control', json={'enabled':True}, headers={'Origin':'https://evil.example'})
        self.assertEqual(response.status, 403)

    async def test_explicit_stop_persists_pause_before_signalling_supervisor(self):
        store = self.app['store']
        room = store.add('https://live.douyin.com/123456789')
        store.update(room['id'], {'enabled':True})
        self.assertEqual((await self.client.post('/api/shutdown?pause=1',json={})).status, 200)
        self.assertFalse(store.get(room['id'])['enabled'])
        self.assertTrue((Path(self.tmp.name)/'supervisor.stop').exists())

    async def test_recording_requires_writable_selected_folder_and_persists(self):
        result = await self.client.post('/api/rooms', json={'url':'https://live.douyin.com/123456789'})
        rid = (await result.json())['room']['id']
        self.assertEqual((await self.client.patch(f'/api/rooms/{rid}', json={'record_enabled':True})).status, 400)
        self.assertFalse(self.app['store'].get(rid)['record_enabled'])
        for value in ['', 'relative/path', str(Path(self.tmp.name)/'missing')]:
            self.assertEqual((await self.client.post('/api/recording-folder', json={'path':value})).status, 400)
        folder = str(Path(self.tmp.name).resolve())
        self.assertEqual((await self.client.post('/api/recording-folder', json={'path':folder})).status, 200)
        self.assertEqual((await self.client.patch(f'/api/rooms/{rid}', json={'record_enabled':True,'enabled':True})).status, 200)
        self.assertEqual((await self.client.post('/api/recording-folder', json={'path':folder})).status, 400)
        state = await (await self.client.get('/api/state')).json()
        self.assertEqual(state['settings']['recordings_dir'], folder)
        import sqlite3
        import contextlib
        with contextlib.closing(sqlite3.connect(Path(self.tmp.name)/'monitor.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT value FROM app_settings WHERE key='recording_dir'").fetchone()[0], folder)

    async def test_history_pagination_date_and_disk_export_do_not_lose_older_text(self):
        result = await self.client.post('/api/rooms', json={'url':'https://live.douyin.com/123456789'})
        rid = (await result.json())['room']['id']
        db = self.app['store'].db
        for i in range(205):
            db.execute('INSERT INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,status,text) VALUES(?,?,?,?,?,?,?,?)',
                (rid,'test',f'{i}.wav',0,30,'2026-09-06T04:00:00+00:00' if i==0 else '2026-09-07T04:00:00+00:00','done',f'中文测试{i}'))
        db.commit()
        page = await (await self.client.get(f'/api/rooms/{rid}/transcripts')).json()
        self.assertEqual(len(page['items']),200)
        self.assertTrue(page['has_more'])
        previous = await (await self.client.get(f'/api/rooms/{rid}/transcripts?before={page["items"][0]["id"]}')).json()
        self.assertEqual(len(previous['items']),5)
        self.assertFalse(previous['has_more'])
        dates = await (await self.client.get(f'/api/rooms/{rid}/transcript-days')).json()
        self.assertEqual(len(dates['days']),2)
        exported = await self.client.get(f'/api/rooms/{rid}/transcript.txt?day=2026-09-06')
        content = await exported.text()
        self.assertIn('中文测试0', content)
        self.assertNotIn('中文测试1', content)
        self.assertEqual((await self.client.get(f'/api/rooms/{rid}/transcripts?day=2026-99-99')).status,400)
        saved = await (await self.client.post(f'/api/rooms/{rid}/save-transcript',json={'day':'2026-09-06'})).json()
        self.assertEqual(Path(saved['path']).read_text(encoding='utf-8'), content)
        self.assertTrue(Path(saved['path']).is_relative_to(Path(self.tmp.name)/'exports'))
        response = await self.client.get('/api/state', headers={'Host':'evil.example'})
        self.assertEqual(response.status, 403)
        response = await self.client.post('/api/control', data='enabled=true')
        self.assertEqual(response.status, 415)
        response = await self.client.get('/api/state', headers={'Host':'evil.example'})
        self.assertEqual(response.status, 403)

    async def test_one_shot_refresh_works_without_enabling_schedule(self):
        response = await self.client.post('/api/rooms', json={'url':'https://live.douyin.com/123456789'})
        rid = (await response.json())['room']['id']
        monitor = self.app['monitor']
        monitor.task = asyncio.create_task(monitor.run())
        for _ in range(40):
            if self.app['store'].get(rid)['last_success']:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.collector.calls, 1)
        self.assertFalse(self.app['store'].get(rid)['enabled'])
        self.assertEqual(self.app['store'].get(rid)['latest']['online'], 12)

    async def test_speech_opt_in_and_export_keep_real_text(self):
        response = await self.client.post('/api/rooms', json={'url':'https://live.douyin.com/123456789'})
        room = (await response.json())['room']; rid=room['id']
        self.assertFalse(room['transcribe_enabled'])
        changed = await self.client.patch(f'/api/rooms/{rid}',json={'transcribe_enabled':True,'enabled':True})
        self.assertTrue((await changed.json())['room']['transcribe_enabled'])
        store=self.app['store']
        store.db.execute("INSERT INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,status,text) VALUES(?,?,?,?,?,?,?,?)",(rid,'synthetic','synthetic.wav',0,30,'2026-09-07T09:00:00+00:00','done','这是合成接口测试文本。'))
        store.db.commit()
        transcript=await (await self.client.get(f'/api/rooms/{rid}/transcripts')).json()
        self.assertEqual(transcript['items'][0]['text'],'这是合成接口测试文本。')
        self.assertNotIn('audio_path',transcript['items'][0])
        export=await self.client.get(f'/api/rooms/{rid}/transcript.txt')
        self.assertIn('这是合成接口测试文本。',await export.text())
        await self.client.delete(f'/api/rooms/{rid}')
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM speech_chunks').fetchone()[0],1)
        self.assertIsNone(store.get(rid))


if __name__ == '__main__':
    unittest.main()
