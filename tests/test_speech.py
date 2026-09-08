import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Store
from speech import AudioRecorder, SpeechService


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.room=self.store.add('https://live.douyin.com/1234567')
        self.speech=SpeechService(self.tmp.name,self.store)
        self.pattern=self.speech.audio._prepare_output(self.room['id'])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def index(self,rows):
        with (self.pattern.parent/'segments.csv').open('w',newline='',encoding='utf-8') as f:
            csv.writer(f).writerows(rows)

    def test_only_closed_listed_chunks_queue_and_scan_is_idempotent(self):
        for i in (0,1):
            (self.pattern.parent/f'chunk-{i:06}.wav').write_bytes(b'synthetic-fixture'*10)
        self.index([['chunk-000000.wav',0,30]])
        self.speech.scan(); self.speech.scan_marks.clear(); self.speech.scan()
        rows=self.store.db.execute('SELECT * FROM speech_chunks').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'pending')
        self.assertEqual(rows[0]['end_seconds'],30)
        self.assertIn('chunk-000000.wav',rows[0]['audio_path'])

    def test_deleted_room_audio_does_not_recreate_transcripts(self):
        (self.pattern.parent/'chunk-000000.wav').write_bytes(b'synthetic-fixture'*10)
        self.index([['chunk-000000.wav',0,30]])
        self.store.delete(self.room['id']);self.speech.scan()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM speech_chunks').fetchone()[0],0)

    def test_index_cannot_read_outside_session_or_invalid_duration(self):
        outside=Path(self.tmp.name)/'outside.wav';outside.write_bytes(b'x'*100)
        local=self.pattern.parent/'chunk-000000.wav';local.write_bytes(b'x'*100)
        self.index([[str(outside),0,30],['chunk-000000.wav',0,300]])
        self.speech.scan()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM speech_chunks').fetchone()[0],0)

    def test_restart_recovers_interrupted_job_and_keeps_opt_in(self):
        (self.pattern.parent/'chunk-000000.wav').write_bytes(b'x'*100)
        self.index([['chunk-000000.wav',0,30]])
        self.speech.scan();self.store.db.execute("UPDATE speech_chunks SET status='processing'");self.store.db.commit()
        self.assertFalse(self.store.get(self.room['id'])['transcribe_enabled'])
        self.store.update(self.room['id'],{'transcribe_enabled':True})
        self.store.close();self.store=Store(self.tmp.name)
        self.assertTrue(self.store.get(self.room['id'])['transcribe_enabled'])
        self.assertEqual(self.store.db.execute('SELECT status FROM speech_chunks').fetchone()[0],'pending')

    def test_audio_is_mono_segmented_and_separate_from_video(self):
        args=self.speech.audio._ffmpeg_args('ffmpeg','https://cdn.example/live.flv',self.pattern)
        self.assertIn('-vn',args)
        self.assertEqual(args[args.index('-segment_time')+1],'30')
        self.assertEqual(args[args.index('-ar')+1],'16000')
        self.assertIn('transcription-audio',str(self.pattern))
        self.assertTrue((self.pattern.parent/'session.json').is_file())


if __name__=='__main__':unittest.main()
