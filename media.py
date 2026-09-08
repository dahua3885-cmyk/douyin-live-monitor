"""Segmented recording, per-broadcast duration limits and restart-safe MP4 queue."""
import asyncio
import csv
from datetime import timedelta
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from archive import now, dt
from recorder import Recorder, DISK_CHECK_SECONDS, STARTUP_TIMEOUT_SECONDS

QUALITY_ORDER = ['ORIGIN','FULL_HD1','HD1','SD1','SD2']
QUALITY_NAMES = {'ORIGIN':'原画','FULL_HD1':'蓝光','HD1':'超清','SD1':'高清','SD2':'标清','AUTO':'自动'}


def choose_quality(snapshot, preferred='SD1'):
    choices=snapshot.get('stream_choices') or {}
    index=QUALITY_ORDER.index(preferred) if preferred in QUALITY_ORDER else 0
    order=QUALITY_ORDER[index:]+list(reversed(QUALITY_ORDER[:index]))
    for quality in order:
        if choices.get(quality):return choices[quality],quality,quality!=preferred
    return snapshot.get('stream_url'),'AUTO',bool(choices) or preferred!='AUTO'


class MediaService:
    def __init__(self,store):
        self.store,self.db=store,store.db
        self.db.execute('CREATE TABLE IF NOT EXISTS recording_runs (id TEXT PRIMARY KEY,broadcast_id TEXT,room_id TEXT,folder TEXT,started_at TEXT,quality TEXT,convert_enabled INTEGER,closed INTEGER DEFAULT 0)')
        if 'output_folder' not in {row[1] for row in self.db.execute('PRAGMA table_info(recording_runs)')}:
            self.db.execute('ALTER TABLE recording_runs ADD COLUMN output_folder TEXT')
        self.db.execute("UPDATE media_assets SET state='pending' WHERE state='converting'")
        self.db.commit()
        self.task=None;self.closed=False;self.process=None;self.scan_stamps={}

    def scan(self):
        for raw in self.db.execute('SELECT * FROM recording_runs').fetchall():
            run=dict(raw);folder=Path(run['folder']);index=folder/'segments.csv'
            try:
                stamp=index.stat().st_mtime_ns
                if self.scan_stamps.get(str(index))==stamp:continue
                for row in csv.reader(index.read_text(encoding='utf-8-sig').splitlines()):
                    if len(row)!=3:continue
                    filename=(folder/row[0]).resolve()
                    if filename.parent!=folder.resolve() or filename.suffix!='.ts' or not filename.is_file():continue
                    start,end=float(row[1]),float(row[2])
                    if not 0<=start<end:continue
                    sid=uuid.uuid5(uuid.NAMESPACE_URL,str(filename)).hex[:24]
                    captured=(dt(run['started_at'])+timedelta(seconds=start)).isoformat(timespec='milliseconds')
                    self.db.execute('INSERT OR IGNORE INTO media_assets(id,broadcast_id,room_id,path,captured_at,duration,bytes,state,convert_enabled,quality) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (sid,run['broadcast_id'],run['room_id'],str(filename),captured,end-start,filename.stat().st_size,'pending' if run['convert_enabled'] else 'saved',run['convert_enabled'],run['quality']))
                self.db.commit();self.scan_stamps[str(index)]=stamp
            except (OSError,ValueError,csv.Error):continue

    async def _process(self,args,timeout=180):
        options={'stdout':asyncio.subprocess.PIPE,'stderr':asyncio.subprocess.PIPE}
        if hasattr(subprocess,'CREATE_NO_WINDOW'):options['creationflags']=subprocess.CREATE_NO_WINDOW
        self.process=await asyncio.create_subprocess_exec(*args,**options)
        try:
            out,err=await asyncio.wait_for(self.process.communicate(),timeout)
            if self.process.returncode:raise RuntimeError('媒体处理未完成，请查看文件或重试。')
            return out
        finally:
            if self.process and self.process.returncode is None:
                self.process.kill();await self.process.wait()
            self.process=None

    async def probe(self,path):
        data=await self._process([shutil.which('ffprobe') or 'ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],30)
        info=json.loads(data)
        duration=float(info.get('format',{}).get('duration') or 0)
        if duration<=0:raise RuntimeError('媒体没有可确认的有效时长')
        return info,duration

    async def convert(self,row):
        source=Path(row['path'])
        run=self.db.execute('SELECT output_folder FROM recording_runs WHERE folder=?',(str(source.parent),)).fetchone()
        target=Path(row['mp4_path']) if row.get('mp4_path') else (Path(run[0])/source.with_suffix('.mp4').name if run and run[0] else source.with_suffix('.mp4'))
        temporary=target.with_suffix('.converting.mp4')
        self.db.execute("UPDATE media_assets SET state='converting',error=NULL WHERE id=?",(row['id'],));self.db.commit()
        try:
            _,source_duration=await self.probe(source)
            if not target.exists():
                await self._process([shutil.which('ffmpeg') or 'ffmpeg','-hide_banner','-loglevel','error','-i',str(source),
                    '-map','0:v?','-map','0:a?','-c','copy','-movflags','+faststart','-y',str(temporary)])
                _,duration=await self.probe(temporary)
                if abs(duration-source_duration)>max(2,source_duration*.01):raise RuntimeError('MP4 时长与原片不一致，保留 TS 待重试')
                temporary.replace(target)
            else:
                _,duration=await self.probe(target)
                if abs(duration-source_duration)>max(2,source_duration*.01):raise RuntimeError('已有 MP4 时长不匹配，请检查文件')
            self.db.execute("UPDATE media_assets SET state='ready',mp4_path=?,duration=?,error=NULL WHERE id=?",(str(target),source_duration,row['id']));self.db.commit()
            self.store.event(row['room_id'],'media_ready','一个录像片段已生成 MP4，可在场次档案查看。')
        except asyncio.CancelledError:
            self.db.execute("UPDATE media_assets SET state='pending' WHERE id=?",(row['id'],));self.db.commit();raise
        except Exception as exc:
            message=str(exc)[:180]
            self.db.execute("UPDATE media_assets SET state='error',error=? WHERE id=?",(message,row['id']));self.db.commit()
            self.store.event(row['room_id'],'media_error','MP4 生成失败，原始 TS 已保留，可在档案重试。')

    async def recover_unclosed(self, include_open=False):
        # Runs from a previous service incarnation have no live writer. Probe
        # orphan tails instead of inventing transcript coverage for missing data.
        for raw in self.db.execute('SELECT * FROM recording_runs WHERE closed IN (2,3)'+(' OR closed=0' if include_open else '')).fetchall():
            run=dict(raw);folder=Path(run['folder']);cursor=0.0
            for file in sorted(folder.glob('part-*.ts')):
                existing=self.db.execute('SELECT duration FROM media_assets WHERE path=?',(str(file.resolve()),)).fetchone()
                if existing:cursor+=existing[0];continue
                try:
                    _,duration=await self.probe(file)
                    self.db.execute('INSERT OR IGNORE INTO media_assets(id,broadcast_id,room_id,path,captured_at,duration,bytes,state,convert_enabled,quality,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex[:24],run['broadcast_id'],run['room_id'],str(file.resolve()),(dt(run['started_at'])+timedelta(seconds=cursor)).isoformat(),duration,file.stat().st_size,'pending' if run['convert_enabled'] else 'saved',run['convert_enabled'],run['quality'],None if run['closed']==3 else '异常退出后恢复的片段，请复核尾部'))
                    cursor+=duration
                except Exception:continue
            self.db.execute('UPDATE recording_runs SET closed=1 WHERE id=?',(run['id'],))
        self.db.commit()

    async def run(self):
        while not self.closed:
            self.scan()
            await self.recover_unclosed()
            row=self.db.execute("SELECT * FROM media_assets WHERE state='pending' ORDER BY captured_at LIMIT 1").fetchone()
            if row:await self.convert(dict(row))
            else:
                from media_cleanup import clean_ready_runs
                await clean_ready_runs(self)
                await asyncio.sleep(1)

    async def close(self):
        self.closed=True
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        self.scan()


class SegmentRecorder(Recorder):
    def __init__(self,data_dir,store,media):
        super().__init__(data_dir,require_selection=True)
        self.store,self.media=store,media
        self.runs={};self.limits={};self.quality={};self.warning_at={};self.generation_session={}

    def _fail(self,entry,message):
        prior=entry.error
        result=Recorder._fail(entry,message)
        rid=next((rid for rid,value in self._entries.items() if value is entry),None)
        if rid and prior!=message:self.store.event(rid,'record_error',message)
        return result

    async def sync(self,room,snapshot):
        room=room|self.store.recording_settings()
        rid=room['id'];sid=room.get('broadcast_id')
        if not room.get('record_enabled'):self.limits.pop(rid,None)
        if not sid:return await super().sync(room|{'record_enabled':False},snapshot)
        previous=self.generation_session.get(rid)
        if previous and previous!=sid:await self.stop(rid)
        self.generation_session[rid]=sid
        entry=self._entries.get(rid)
        if entry and entry.process and entry.process.returncode is None:
            # A running writer keeps the parameters it started with. New global
            # settings apply when the next recording run starts.
            active={k:v for k,v in self.contexts.get(rid,{}).items() if k in self.store.recording_settings() or k=='remaining_seconds'}
            return await super().sync(room|active,snapshot)
        url,quality,fallback=choose_quality(snapshot,room.get('record_quality','SD1'))
        self.quality[rid]={'selected':quality,'preferred':room.get('record_quality'),'fallback':fallback}
        self.media.scan()
        total=self.store.db.execute('SELECT COALESCE(SUM(duration),0) FROM media_assets WHERE broadcast_id=?',(sid,)).fetchone()[0]
        limit=room.get('record_limit_minutes',0)*60
        if limit and total>=limit-1 and room.get('record_enabled') and snapshot.get('status')=='live':
            await self.stop(rid)
            self.limits[rid]=sid
            entry=self._entries.get(rid)
            if entry:entry.state='limit'
            return self.status(rid)
        self.limits.pop(rid,None)
        self.contexts[rid]=dict(room,remaining_seconds=max(1,int(limit-total)) if limit else 0)
        return await super().sync(self.contexts[rid],snapshot|{'stream_url':url})

    def _prepare_output(self,rid):
        room=self.contexts[rid];sid=room['broadcast_id'];stamp=now()
        name=room.get('name') or (room.get('latest') or {}).get('nickname') or '直播间'
        name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',name).strip('. ')[:60] or '直播间'
        root=self.recordings_dir.resolve()
        run_id=uuid.uuid4().hex[:16]
        folder=(root/f'{name}_{rid}'/f'{dt(stamp).astimezone():%Y-%m-%d}_{sid}'/run_id).resolve()
        if not folder.is_relative_to(root):raise ValueError('录制目录无效')
        folder.mkdir(parents=True,exist_ok=False)
        meta={'run_id':run_id,'broadcast_id':sid,'room_id':rid,'started_at':stamp,'segment_minutes':room.get('segment_minutes',0)}
        (folder/'recording.json').write_text(json.dumps(meta,ensure_ascii=False),encoding='utf-8')
        self.runs[rid]=meta|{'folder':str(folder)}
        return folder/('part-%06d.ts' if room.get('segment_minutes',0) else 'part-000000.ts')

    def output_prepared(self,rid,output):
        run=self.runs[rid];room=self.contexts[rid]
        self.store.db.execute('INSERT INTO recording_runs(id,broadcast_id,room_id,folder,started_at,quality,convert_enabled) VALUES(?,?,?,?,?,?,?)',
            (run['run_id'],run['broadcast_id'],rid,run['folder'],run['started_at'],self.quality.get(rid,{}).get('selected','AUTO'),int(room.get('convert_mp4',True))))
        self.store.db.commit()

    def _ffmpeg_args(self,ffmpeg,url,output):
        rid=next(rid for rid,run in self.runs.items() if Path(run['folder'])==output.parent)
        room=self.contexts[rid]
        args=[ffmpeg,'-hide_banner','-nostats','-loglevel','error','-protocol_whitelist','http,https,tcp,tls,crypto',
            '-rw_timeout','15000000','-i',url,'-map','0:v?','-map','0:a?','-c','copy']
        if room.get('remaining_seconds'):args+=['-t',str(room['remaining_seconds'])]
        minutes=room.get('segment_minutes',0)
        if minutes:
            args+=['-f','segment','-segment_format','mpegts','-segment_time',str(minutes*60),
                '-reset_timestamps','1','-segment_list',str(output.parent/'segments.csv'),'-segment_list_type','csv','-n',str(output)]
        else:
            args+=['-f','mpegts','-n',str(output)]
        return tuple(args)

    @staticmethod
    def _has_output(filename):
        return any(p.stat().st_size>0 for p in Path(filename).parent.glob('part-*.ts'))

    def status(self,rid):
        result=super().status(rid);run=self.runs.get(rid)
        if rid in self.limits:result.update(state='limit',error=None)
        if run:
            files=list(Path(run['folder']).glob('part-*.ts'))
            result['bytes']=sum(f.stat().st_size for f in files if f.exists())
            result['last_saved_at']=max((f.stat().st_mtime for f in files if f.exists()),default=None)
        result['quality']=self.quality.get(rid,{})
        return result

    async def _watch(self,rid,entry,process):
        began=time.monotonic();disk_check=0
        try:
            while entry.process is process and not entry.stopping:
                if process.returncode is not None:
                    async with self._lock(rid):
                        if entry.process is process and not entry.stopping:
                            limit=self.contexts[rid].get('remaining_seconds',0)
                            elapsed=time.monotonic()-began
                            completed=process.returncode==0 and limit and elapsed>=max(0,limit-5)
                            await self._stop_entry(entry,error=None if completed else f'直播录制意外停止（代码 {process.returncode}），下轮将重新尝试。')
                            if completed:
                                entry.state='limit';self.limits[rid]=self.contexts[rid]['broadcast_id']
                                self.store.event(rid,'record_limit','已达到本场设定录制时长，停止录制；监控与话术采集继续。')
                    return
                current=time.monotonic()
                if current>=disk_check:
                    enough=await self._has_space()
                    usage=await asyncio.to_thread(shutil.disk_usage,self.recordings_dir)
                    if usage.free<5*1024**3 and current-self.warning_at.get(rid,-1e9)>600:
                        self.store.event(rid,'disk_warning','录制磁盘剩余空间低于 5 GB，请及时处理。');self.warning_at[rid]=current
                    if not enough:
                        async with self._lock(rid):await self._stop_entry(entry,error='磁盘剩余空间不足 512 MB，已停止录制。')
                        return
                    disk_check=current+DISK_CHECK_SECONDS
                if entry.state=='idle':
                    if await asyncio.to_thread(self._has_output,entry.file):
                        entry.state='recording';entry.started_at=now()
                        self.store.db.execute("UPDATE archive_gaps SET ended_at=? WHERE broadcast_id=? AND kind='recording' AND ended_at IS NULL",(entry.started_at,self.contexts[rid]['broadcast_id']));self.store.db.commit()
                        self.store.event(rid,'recording','已收到音视频并开始写入录像文件。')
                    elif current-began>=STARTUP_TIMEOUT_SECONDS:
                        async with self._lock(rid):await self._stop_entry(entry,error='启动后未收到音视频，稍后重试。')
                        return
                await asyncio.sleep(.5)
        except asyncio.CancelledError:raise
        except Exception:
            async with self._lock(rid):await self._stop_entry(entry,error='录制监测异常，已停止并保留已写入片段。')

    async def _stop_entry(self,entry,error=None):
        rid=next((rid for rid,value in self._entries.items() if value is entry),None)
        active=entry.process is not None
        await super()._stop_entry(entry,error)
        if active and rid:
            run=self.runs.get(rid)
            if run:
                self.media.scan_stamps.clear();self.media.scan()
                closed=3 if not error and run.get('segment_minutes',0)==0 else 2
                self.store.db.execute('UPDATE recording_runs SET closed=? WHERE id=?',(closed,run['run_id']));self.store.db.commit()
                if error:
                    self.store.db.execute('INSERT INTO archive_gaps(broadcast_id,started_at,ended_at,kind,reason) VALUES(?,?,NULL,?,?)',(run['broadcast_id'],now(),'recording',error));self.store.db.commit()
            self.store.event(rid,'record_error' if error else 'record_stopped',error or '视频录制已停止，已保存的片段正在归档。')
