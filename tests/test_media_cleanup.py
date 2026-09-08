import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock,patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Store
from media import MediaService
from media_cleanup import clean_ready_runs
from recording_locations import recording_location


class CleanupTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root/'data');self.media=MediaService(self.store)
        self.room=self.store.add('https://live.douyin.com/123456789')
        self.public=self.root/'videos';self.public.mkdir()
        self.ts=self.public/'part-000000.ts';self.ts.write_bytes(b'original recording')
        self.mp4=self.public/'part-000000.mp4';self.mp4.write_bytes(b'valid mp4 fixture')
        (self.public/'segments.csv').write_text('part-000000.ts,0,10\n')
        (self.public/'recording.json').write_text('{}')
        self.store.db.execute('INSERT INTO recording_runs(id,broadcast_id,room_id,folder,started_at,quality,convert_enabled,closed) VALUES(?,?,?,?,?,?,?,?)',('finished','broadcast',self.room['id'],str(self.public),'2026-09-08T00:00:00+00:00','SD1',1,1))
        self.store.db.execute('INSERT INTO media_assets(id,broadcast_id,room_id,path,mp4_path,captured_at,duration,state) VALUES(?,?,?,?,?,?,?,?)',('asset','broadcast',self.room['id'],str(self.ts),str(self.mp4),'2026-09-08T00:00:00+00:00',10,'ready'));self.store.db.commit()
        self.probe=patch.object(self.media,'probe',new_callable=AsyncMock,return_value=({},10));self.probe.start()

    async def asyncTearDown(self):
        self.probe.stop();await self.media.close();self.store.close();self.tmp.cleanup()

    async def test_verified_finished_output_contains_only_mp4(self):
        await clean_ready_runs(self.media)
        self.assertEqual([p.name for p in self.public.iterdir()],['part-000000.mp4'])
        asset=self.store.db.execute('SELECT * FROM media_assets').fetchone()
        self.assertEqual(Path(asset['path']).read_bytes(),b'original recording')
        self.assertEqual(asset['mp4_path'],str(self.mp4))
        self.assertEqual(recording_location(self.store,SimpleNamespace(runs={}),room_id=self.room['id']),self.public.resolve())
        self.media.scan();self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM media_assets').fetchone()[0],1)

    async def test_active_or_failed_conversion_never_moves_source(self):
        self.store.db.execute('UPDATE recording_runs SET closed=0');self.store.db.commit()
        await clean_ready_runs(self.media);self.assertTrue(self.ts.exists())
        self.store.db.execute('UPDATE recording_runs SET closed=1');self.store.db.execute("UPDATE media_assets SET state='error'");self.store.db.commit()
        await clean_ready_runs(self.media);self.assertTrue(self.ts.exists())
        self.assertEqual(len(list(self.public.iterdir())),4)

    async def test_conflicting_backup_does_not_overwrite_or_remove_original(self):
        backup=self.store.data_dir/'recording-sources'/'finished';backup.mkdir(parents=True)
        (backup/self.ts.name).write_bytes(b'other content')
        await clean_ready_runs(self.media)
        self.assertEqual(self.ts.read_bytes(),b'original recording')
        self.assertEqual((backup/self.ts.name).read_bytes(),b'other content')
        self.assertIsNone(self.store.db.execute('SELECT output_folder FROM recording_runs').fetchone()[0])

    async def test_interrupted_duplicate_cleanup_can_resume(self):
        with patch('media_cleanup.remove_duplicate',side_effect=OSError('file busy')):
            await clean_ready_runs(self.media)
        self.assertTrue(self.ts.exists())
        await clean_ready_runs(self.media)
        self.assertEqual([p.name for p in self.public.iterdir()],['part-000000.mp4'])
