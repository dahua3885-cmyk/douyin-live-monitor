import asyncio
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Store,create_app
from archive import Archive
from media import MediaService,SegmentRecorder,choose_quality
from notifications import Notifications,payload,validate_url
from test_api import FakeCollector,FakeRecorder


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.archive=Archive(self.store)
        self.room=self.store.add('https://live.douyin.com/123456789','测试主播')
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def snap(self,stamp='2026-09-07T23:59:00+00:00',**changes):
        return {'status':'live','room_id':'100','started_at':'2026-09-07T23:00:00+00:00','observed_at':stamp,'nickname':'测试主播',**changes}
    def test_reconnect_and_midnight_preserve_broadcast_but_new_platform_id_splits(self):
        a=self.archive.on_observation(self.room,self.snap())
        b=self.archive.on_observation(self.room,self.snap('2026-09-08T00:03:00+00:00'))
        self.assertEqual(a,b);self.assertTrue(self.archive.detail(a)['gaps'])
        c=self.archive.on_observation(self.room,self.snap('2026-09-08T00:04:00+00:00',room_id='200'))
        self.assertNotEqual(a,c);self.assertEqual(self.archive.get(a)['state'],'ended')
    def test_unknown_does_not_end_broadcast_but_offline_does(self):
        sid=self.archive.on_observation(self.room,self.snap())
        self.archive.on_observation(self.room,self.snap('2026-09-08T00:00:00+00:00',status='error'))
        self.assertEqual(self.archive.get(sid)['state'],'live')
        self.archive.on_observation(self.room,self.snap('2026-09-08T00:01:00+00:00',status='offline'))
        self.assertEqual(self.archive.get(sid)['state'],'ended')
    def test_search_matches_segment_time_and_video_offset_without_inventing_missing_video(self):
        sid=self.archive.on_observation(self.room,self.snap('2026-09-08T00:00:00+00:00'))
        self.store.db.execute('INSERT INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,status,text,segments,broadcast_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (self.room['id'],'audio','test.wav',0,30,'2026-09-08T00:00:10+00:00','done','欢迎大家，报名领取资料',json.dumps([{'start':8,'text':'报名领取资料'}]),sid))
        self.store.db.execute('INSERT INTO media_assets(id,broadcast_id,room_id,path,captured_at,duration,state,mp4_path) VALUES(?,?,?,?,?,?,?,?)',
            ('video',sid,self.room['id'],'test.ts','2026-09-08T00:00:00+00:00',30,'ready','test.mp4'));self.store.db.commit()
        row=self.archive.speech(sid,'报名')[0]
        self.assertEqual(row['video']['offset'],18)
        result=self.archive.analyze(sid)
        self.assertIn('规则',result['method']);self.assertEqual(result['timeline'][0]['category'],'转化')
        self.assertEqual(result['timeline'][0]['quote'],'欢迎大家，报名领取资料')
        self.store.db.execute("UPDATE media_assets SET state='error'")
        self.assertIsNone(self.archive.speech(sid,'报名')[0]['video'])
    def test_legacy_capture_is_labelled_not_fabricated_broadcast(self):
        self.store.db.execute('INSERT INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,status,text) VALUES(?,?,?,?,?,?,?,?)',
            (self.room['id'],'oldcapture','test.wav',0,30,'2026-09-07T12:00:00+00:00','done','旧记录'))
        self.store.db.commit();self.store.close();self.store=Store(self.tmp.name);self.archive=Archive(self.store)
        item=self.archive.list()[0];self.assertEqual(item['legacy'],1);self.assertEqual(item['state'],'legacy')
        self.store.delete(self.room['id']);self.assertEqual(len(self.archive.list()),1)
    def test_quality_falls_down_then_uses_available_higher(self):
        self.assertEqual(choose_quality({'stream_choices':{'SD1':'a','SD2':'b'}},'HD1'),('a','SD1',True))
        self.assertEqual(choose_quality({'stream_choices':{'ORIGIN':'o'}},'SD1'),('o','ORIGIN',True))
    def test_unknown_speech_remains_unclassified(self):
        sid=self.archive.on_observation(self.room,self.snap())
        self.store.db.execute('INSERT INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,status,text,broadcast_id) VALUES(?,?,?,?,?,?,?,?,?)',
            (self.room['id'],'a','a.wav',0,30,self.snap()['observed_at'],'done','今天刮了一阵风',sid));self.store.db.commit()
        self.assertEqual(self.archive.analyze(sid)['timeline'][0]['category'],'待人工判断')

    def test_legacy_group_combines_reading_without_rewriting_original_sessions(self):
        for i,stamp in enumerate(['2026-09-07T12:00:00+00:00','2026-09-07T13:00:00+00:00']):
            self.store.db.execute('INSERT INTO speech_chunks(room_id,session_id,audio_path,start_seconds,end_seconds,captured_at,status,text) VALUES(?,?,?,?,?,?,?,?)',
                (self.room['id'],f'old{i}',f'a{i}.wav',0,30,stamp,'done',f'报名测试{i}'))
        self.store.db.commit();self.store.close();self.store=Store(self.tmp.name);self.archive=Archive(self.store)
        originals=self.archive.list();grouped=self.archive.list(grouped=True)
        self.assertEqual(len(originals),2);self.assertEqual(len(grouped),1)
        sid=grouped[0]['id'];self.assertEqual(grouped[0]['fragment_count'],2)
        self.assertEqual(len(self.archive.speech(sid,'报名')),2)
        self.assertEqual(self.archive.detail(sid)['speech_seconds'],60)
        self.assertTrue(self.archive.detail(sid)['gaps'])
        self.assertEqual(len(self.archive.analyze(sid)['timeline']),2)
        self.assertEqual(len(self.archive.list()),2)


class FeatureApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(self.tmp.name,collector=FakeCollector(),recorder=FakeRecorder(),start_worker=False)
        self.server=TestServer(self.app);self.client=TestClient(self.server);await self.client.start_server();self.app['port']=self.server.port
    async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()
    async def test_import_deduplicates_and_exports_without_recording(self):
        r=await(self.client.post('/api/rooms/import',json={'text':'一号 https://live.douyin.com/123456789\nhttps://live.douyin.com/123456789/\nnot a url'}))
        body=await r.json();self.assertEqual(len(body['added']),1);self.assertEqual(len(body['duplicates']),1);self.assertEqual(len(body['errors']),1)
        self.assertFalse(body['added'][0]['record_enabled'])
        export=await(await self.client.get('/api/rooms/export')).json()
        self.assertEqual(export['rooms'][0]['name'],'一号')
    async def test_batch_validation_is_atomic_and_enforces_record_folder(self):
        store=self.app['store'];a=store.add('https://live.douyin.com/123456789');b=store.add('https://live.douyin.com/987654321')
        r=await self.client.post('/api/rooms/bulk',json={'ids':[a['id'],b['id']],'changes':{'record_enabled':True}})
        self.assertEqual(r.status,400);self.assertFalse(store.get(a['id'])['record_enabled'])
        r=await self.client.post('/api/rooms/bulk',json={'ids':[a['id'],'missing'],'changes':{'enabled':True}})
        self.assertEqual(r.status,404);self.assertFalse(store.get(a['id'])['enabled'])
        r=await self.client.post('/api/rooms/bulk',json={'ids':[a['id'],b['id']],'changes':{'record_quality':'SD2'}})
        self.assertEqual(r.status,400)
        r=await self.client.post('/api/recording-settings',json={'record_quality':'SD2','segment_minutes':15,'record_limit_minutes':60})
        self.assertEqual(r.status,200)
        self.assertEqual(store.get(a['id'])['record_quality'],'SD2');self.assertEqual(store.get(b['id'])['record_quality'],'SD2')
    async def test_archive_media_supports_ranges_and_rejects_arbitrary_paths(self):
        store=self.app['store'];r=store.add('https://live.douyin.com/123456789');sid=Archive(store).on_observation(r,{'status':'live','room_id':'1'})
        p=Path(self.tmp.name)/'test.mp4';p.write_bytes(b'0123456789')
        store.db.execute('INSERT INTO media_assets(id,broadcast_id,room_id,path,mp4_path,captured_at,duration,state) VALUES(?,?,?,?,?,?,?,?)',('asset',sid,r['id'],str(p),str(p),'2026-09-08T00:00:00+00:00',10,'ready'));store.db.commit()
        response=await self.client.get('/api/media/asset',headers={'Range':'bytes=2-4'})
        self.assertEqual(response.status,206);self.assertEqual(await response.read(),b'234')
        self.assertEqual((await self.client.get('/api/media/missing')).status,404)
        self.assertEqual((await self.client.post('/api/archives/'+sid+'/analyze',json={})).status,200)
    async def test_notification_settings_mask_secrets_and_do_not_send_on_save(self):
        with patch.object(Notifications,'send',new_callable=AsyncMock) as send:
            response=await self.client.post('/api/push',json={'enabled':True,'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/abcdefghijk','secret':'private-secret','events':['live']})
            self.assertEqual(response.status,200);text=await response.text();self.assertNotIn('private-secret',text);self.assertNotIn('abcdefghijk',text);send.assert_not_awaited()
            self.assertEqual((await self.client.post('/api/push/test',json={})).status,200);send.assert_awaited_once()
        self.assertEqual((await self.client.post('/api/push',json={'enabled':True,'webhook':'http://127.0.0.1/','events':['live']})).status,400)

    async def test_ended_broadcast_auto_analyzes_and_notifies_once(self):
        store=self.app['store'];room=store.add('https://live.douyin.com/123456789');archive=Archive(store)
        sid=archive.on_observation(room,{'status':'live','room_id':'p'})
        archive.on_observation(room,{'status':'offline'})
        archive.task=asyncio.create_task(archive.run());await asyncio.sleep(.05);await archive.close()
        self.assertIsNotNone(store.db.execute('SELECT 1 FROM archive_analyses WHERE broadcast_id=?',(sid,)).fetchone())
        second=Archive(store);second.task=asyncio.create_task(second.run());await asyncio.sleep(.05);await second.close()
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events WHERE kind='archive_ready'").fetchone()[0],1)

    async def test_push_queue_does_not_replay_old_or_successfully_sent_events(self):
        store=self.app['store'];notifier=Notifications(store)
        store.event(None,'live','旧事件')
        notifier.save({'enabled':True,'webhook':'https://open.feishu.cn/open-apis/bot/v2/hook/abcdefghijk','events':['live']})
        store.event(None,'live','新事件')
        with patch.object(Notifications,'send',new_callable=AsyncMock) as send:
            notifier.task=asyncio.create_task(notifier.run());await asyncio.sleep(.05);await notifier.close()
            self.assertEqual(send.await_count,1);self.assertIn('新事件',send.call_args.args[0]);self.assertNotIn('旧事件',send.call_args.args[0])
            again=Notifications(store);again.task=asyncio.create_task(again.run());await asyncio.sleep(.05);await again.close()
            self.assertEqual(send.await_count,1)

    async def test_batch_remove_is_validated_and_preserves_archives(self):
        store=self.app['store'];one=store.add('https://live.douyin.com/123456789');two=store.add('https://live.douyin.com/987654321')
        archive=Archive(store);sid=archive.on_observation(one,{'status':'live','room_id':'one'})
        bad=await self.client.post('/api/rooms/bulk-remove',json={'ids':[one['id'],'missing']})
        self.assertEqual(bad.status,404);self.assertIsNotNone(store.get(one['id']))
        response=await self.client.post('/api/rooms/bulk-remove',json={'ids':[one['id'],two['id']]})
        self.assertEqual(response.status,200);self.assertEqual((await response.json())['removed'],2)
        self.assertEqual(store.rooms(),[]);self.assertEqual(archive.get(sid)['room_id'],one['id'])

    async def test_global_recording_settings_work_without_rooms_and_persist_for_new_rooms(self):
        desired={'record_quality':'HD1','segment_minutes':60,'record_limit_minutes':180,'convert_mp4':False}
        response=await self.client.post('/api/recording-settings',json=desired)
        self.assertEqual(response.status,200)
        store=self.app['store'];room=store.add('https://live.douyin.com/123456789')
        for key,value in desired.items():self.assertEqual(store.get(room['id'])[key],value)
        state=await(await self.client.get('/api/state')).json()
        self.assertEqual(state['settings']['recording'],desired)
        self.assertFalse(state['rooms'][0]['record_enabled'])
        self.assertEqual((await self.client.patch('/api/rooms/'+room['id'],json={'record_limit_minutes':30})).status,400)
        for invalid in [{'record_limit_minutes':721},{'convert_mp4':'yes'},{'segment_minutes':0}]:
            self.assertEqual((await self.client.post('/api/recording-settings',json=invalid)).status,400)
        self.assertEqual(store.recording_settings(),desired)
        with __import__('contextlib').closing(Store(self.tmp.name)) as reopened:
            self.assertEqual(reopened.recording_settings(),desired)


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.media=MediaService(self.store);self.rec=SegmentRecorder(Path(self.tmp.name),self.store,self.media)
    async def asyncTearDown(self):await self.rec.close();await self.media.close();self.store.close();self.tmp.cleanup()
    async def test_csv_only_indexes_closed_segments_and_survives_rescan(self):
        folder=Path(self.tmp.name)/'run';folder.mkdir();(folder/'part-000000.ts').write_bytes(b'closed');(folder/'part-000001.ts').write_bytes(b'growing')
        (folder/'segments.csv').write_text('part-000000.ts,0,30\n',encoding='utf-8')
        self.store.db.execute('INSERT INTO recording_runs(id,broadcast_id,room_id,folder,started_at,quality,convert_enabled) VALUES(?,?,?,?,?,?,?)',('run','session','room',str(folder),'2026-09-08T00:00:00+00:00','SD1',1));self.store.db.commit()
        self.media.scan();self.media.scan_stamps.clear();self.media.scan()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0],1)
    async def test_completed_quota_does_not_restart_even_after_service_restart(self):
        room=self.store.add('https://live.douyin.com/123456789');sid=Archive(self.store).on_observation(room,{'status':'live','room_id':'p'})
        self.store.db.execute('INSERT INTO media_assets(id,broadcast_id,room_id,path,captured_at,duration,state) VALUES(?,?,?,?,?,?,?)',('a',sid,room['id'],'test.ts','2026-09-08T00:00:00+00:00',1800,'saved'));self.store.db.commit()
        self.store.save_recording_settings({'record_limit_minutes':30})
        with patch('asyncio.create_subprocess_exec',new_callable=AsyncMock) as spawn:
            result=await self.rec.sync(room|{'broadcast_id':sid,'enabled':True,'record_enabled':True,'record_limit_minutes':30},{'status':'live'})
            self.assertEqual(result['state'],'limit');spawn.assert_not_awaited()

    async def test_active_writer_keeps_its_start_parameters_after_global_change(self):
        from recorder import _Recording
        from test_recorder import _Process
        room=self.store.add('https://live.douyin.com/123456789');rid=room['id']
        self.rec._entries[rid]=_Recording(state='recording',process=_Process())
        self.rec.generation_session[rid]='broadcast'
        self.rec.contexts[rid]=dict(room,broadcast_id='broadcast',record_limit_minutes=60,segment_minutes=15,record_quality='SD1',remaining_seconds=1800)
        self.store.save_recording_settings({'record_limit_minutes':180,'segment_minutes':60,'record_quality':'ORIGIN'})
        await self.rec.sync(room|{'broadcast_id':'broadcast','enabled':True,'record_enabled':True},{'status':'live'})
        self.assertEqual(self.rec.contexts[rid]['record_limit_minutes'],60)
        self.assertEqual(self.rec.contexts[rid]['remaining_seconds'],1800)
    async def test_failed_conversion_keeps_original(self):
        path=Path(self.tmp.name)/'source.ts';path.write_bytes(b'corrupt-but-retain')
        self.store.db.execute('INSERT INTO media_assets(id,broadcast_id,room_id,path,captured_at,duration,state) VALUES(?,?,?,?,?,?,?)',('a','b','r',str(path),'2026-09-08T00:00:00+00:00',10,'pending'));self.store.db.commit()
        with patch.object(self.media,'probe',side_effect=RuntimeError('invalid')):
            await self.media.convert(dict(self.store.db.execute('SELECT * FROM media_assets').fetchone()))
        self.assertEqual(path.read_bytes(),b'corrupt-but-retain');self.assertEqual(self.store.db.execute('SELECT state FROM media_assets').fetchone()[0],'error')


if __name__=='__main__':unittest.main()
