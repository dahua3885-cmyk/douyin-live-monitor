"""Broadcast identity, durable archive, evidence-based speech structure and seek links."""
from datetime import datetime, timezone, timedelta
import asyncio
import json
import re
import uuid
import hashlib
import time


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def dt(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(rooms)')}
    for key, spec in {'deleted':'INTEGER NOT NULL DEFAULT 0','segment_minutes':'INTEGER NOT NULL DEFAULT 30',
                      'record_limit_minutes':'INTEGER NOT NULL DEFAULT 0','record_quality':"TEXT NOT NULL DEFAULT 'SD1'",
                      'convert_mp4':'INTEGER NOT NULL DEFAULT 1','group_name':"TEXT NOT NULL DEFAULT ''"}.items():
        if key not in columns:
            db.execute(f'ALTER TABLE rooms ADD COLUMN {key} {spec}')
    for table in ('speech_chunks','observations'):
        if 'broadcast_id' not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN broadcast_id TEXT')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS broadcasts (
      id TEXT PRIMARY KEY, room_id TEXT NOT NULL, platform_id TEXT, platform_started TEXT,
      name TEXT NOT NULL, title TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
      ended_at TEXT, state TEXT NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, stop_reason TEXT);
    CREATE INDEX IF NOT EXISTS broadcasts_room ON broadcasts(room_id,first_seen);
    CREATE TABLE IF NOT EXISTS archive_gaps (
      id INTEGER PRIMARY KEY, broadcast_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
      kind TEXT NOT NULL, reason TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS media_assets (
      id TEXT PRIMARY KEY, broadcast_id TEXT NOT NULL, room_id TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
      captured_at TEXT NOT NULL, duration REAL NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
      state TEXT NOT NULL, mp4_path TEXT, convert_enabled INTEGER NOT NULL DEFAULT 1, error TEXT, quality TEXT);
    CREATE INDEX IF NOT EXISTS media_broadcast ON media_assets(broadcast_id,captured_at);
    CREATE TABLE IF NOT EXISTS archive_analyses (
      broadcast_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    # Old capture IDs are retained as explicitly labelled fragments, not fabricated broadcasts.
    for row in db.execute('SELECT room_id,session_id,MIN(captured_at) first,MAX(captured_at) last FROM speech_chunks WHERE broadcast_id IS NULL GROUP BY room_id,session_id').fetchall():
        sid = 'legacy-'+uuid.uuid5(uuid.NAMESPACE_URL,row['room_id']+row['session_id']).hex[:20]
        room = db.execute('SELECT name,latest FROM rooms WHERE id=?',(row['room_id'],)).fetchone()
        latest = json.loads(room['latest'] or '{}') if room else {}
        name = (room['name'] if room else '') or latest.get('nickname') or '历史直播间'
        db.execute('INSERT OR IGNORE INTO broadcasts(id,room_id,name,title,first_seen,last_seen,ended_at,state,legacy,stop_reason) VALUES(?,?,?,?,?,?,?,?,1,?)',
            (sid,row['room_id'],name,'升级前采音片段',row['first'],row['last'],row['last'],'legacy','旧版未记录真实场次，保留为历史片段'))
        db.execute('UPDATE speech_chunks SET broadcast_id=? WHERE room_id=? AND session_id=? AND broadcast_id IS NULL',(sid,row['room_id'],row['session_id']))
    for old in db.execute('SELECT id,room_id,first_seen FROM broadcasts WHERE legacy=1').fetchall():
        tail=db.execute('SELECT captured_at,end_seconds-start_seconds duration FROM speech_chunks WHERE broadcast_id=? ORDER BY captured_at DESC LIMIT 1',(old['id'],)).fetchone()
        if tail:
            end=(dt(tail['captured_at'])+timedelta(seconds=tail['duration'])).isoformat(timespec='milliseconds')
            db.execute('UPDATE broadcasts SET last_seen=?,ended_at=? WHERE id=?',(end,end,old['id']))
            db.execute('UPDATE observations SET broadcast_id=? WHERE broadcast_id IS NULL AND room_id=? AND observed_at>=? AND observed_at<?',(old['id'],old['room_id'],old['first_seen'],end))
    db.commit()


class Archive:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.task=None
        self.closed=False
        self.live_checks={}
        if 'ready_notified' not in {r[1] for r in self.db.execute('PRAGMA table_info(broadcasts)')}:
            self.db.execute('ALTER TABLE broadcasts ADD COLUMN ready_notified INTEGER NOT NULL DEFAULT 0')
            self.db.commit()

    def source_key(self,sid):
        if sid.startswith('history-'):
            return json.dumps([(i,self.source_key(i)) for i in self.member_ids(sid)])
        rows=[list(r) for r in self.db.execute('SELECT id,status,text,captured_at,start_seconds,end_seconds FROM speech_chunks WHERE broadcast_id=? ORDER BY id',(sid,))]
        media=[list(r) for r in self.db.execute('SELECT id,state,mp4_path,captured_at,duration FROM media_assets WHERE broadcast_id=? ORDER BY id',(sid,))]
        return 'rules-v2:'+hashlib.sha256(json.dumps([rows,media],ensure_ascii=False).encode()).hexdigest()

    def analysis(self,sid):
        info=self.get(sid);key=self.source_key(sid)
        saved=self.db.execute('SELECT source_key,result FROM archive_analyses WHERE broadcast_id=?',(sid,)).fetchone()
        cached=bool(saved and saved['source_key']==key)
        result=json.loads(saved['result']) if cached else self.analyze(sid)
        ids=self.member_ids(sid) if sid.startswith('history-') else [sid]
        pending=sum(self.db.execute("SELECT COUNT(*) FROM speech_chunks WHERE broadcast_id=? AND status IN ('pending','processing')",(i,)).fetchone()[0] for i in ids)
        return {'result':result,'cached':cached,'is_live':info['state']=='live','pending_chunks':pending,'updated_at':result['created_at']}

    async def run(self):
        while not self.closed:
            for row in self.db.execute("SELECT id,room_id,state,ready_notified FROM broadcasts WHERE state IN ('live','ended') AND legacy=0").fetchall():
                sid=row['id']
                if row['state']=='live':
                    stamp=time.monotonic()
                    if stamp-self.live_checks.get(sid,-1e9)<60:continue
                    self.live_checks[sid]=stamp
                    if self.db.execute("SELECT 1 FROM speech_chunks WHERE broadcast_id=? AND status='done' LIMIT 1",(sid,)).fetchone():self.analysis(sid)
                    continue
                waiting=self.db.execute("SELECT 1 FROM speech_chunks WHERE broadcast_id=? AND status IN ('pending','processing') LIMIT 1",(sid,)).fetchone()
                waiting=waiting or self.db.execute("SELECT 1 FROM media_assets WHERE broadcast_id=? AND state IN ('pending','converting') LIMIT 1",(sid,)).fetchone()
                waiting=waiting or self.db.execute('SELECT 1 FROM recording_runs WHERE broadcast_id=? AND closed IN (0,2,3) LIMIT 1',(sid,)).fetchone()
                if waiting:continue
                old=self.db.execute('SELECT source_key FROM archive_analyses WHERE broadcast_id=?',(sid,)).fetchone()
                if not old or old[0]!=self.source_key(sid):self.analyze(sid)
                if not row['ready_notified']:
                    self.db.execute('UPDATE broadcasts SET ready_notified=1 WHERE id=?',(sid,));self.db.commit()
                    self.store.event(row['room_id'],'archive_ready','本场已采集资料归档完成，结构拆解初稿可在直播档案查看；缺口与失败项一并保留。')
            await asyncio.sleep(5)

    async def close(self):
        self.closed=True
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)

    def current(self, rid):
        row = self.db.execute("SELECT * FROM broadcasts WHERE room_id=? AND state='live' ORDER BY first_seen DESC LIMIT 1",(rid,)).fetchone()
        return dict(row) if row else None

    def on_observation(self, room, snap):
        stamp = snap.get('observed_at') or now()
        current = self.current(room['id'])
        state = snap.get('status')
        if state == 'live':
            platform = str(snap.get('room_id') or '')
            started = snap.get('started_at')
            different = current and ((platform and current['platform_id'] and platform != current['platform_id']) or
                (started and current['platform_started'] and started != current['platform_started']))
            if different:
                self.finish(current['id'],stamp,'检测到新的平台直播场次')
                current = None
            if not current:
                sid = uuid.uuid4().hex[:20]
                name = room['name'] or snap.get('nickname') or '未命名直播间'
                self.db.execute('INSERT INTO broadcasts(id,room_id,platform_id,platform_started,name,title,first_seen,last_seen,state) VALUES(?,?,?,?,?,?,?,?,?)',
                    (sid,room['id'],platform or None,started,name,snap.get('title'),stamp,stamp,'live'))
                current = self.current(room['id'])
            elif (dt(stamp)-dt(current['last_seen'])).total_seconds() > max(90,room['interval_seconds']*3):
                self.gap(current['id'],current['last_seen'],stamp,'monitoring','两次有效观测之间存在空白，无法确认这期间是否连续直播')
            self.db.execute('UPDATE broadcasts SET last_seen=?,platform_id=COALESCE(platform_id,?),platform_started=COALESCE(platform_started,?),name=?,title=? WHERE id=?',
                (stamp,platform or None,started,room['name'] or snap.get('nickname') or current['name'],snap.get('title') or current['title'],current['id']))
            self.db.execute("UPDATE archive_gaps SET ended_at=? WHERE broadcast_id=? AND ended_at IS NULL AND kind IN ('collection','monitoring','pause')",(stamp,current['id']))
        elif current and state == 'offline':
            self.finish(current['id'],stamp,'平台确认下播')
        elif current:
            if not self.db.execute('SELECT 1 FROM archive_gaps WHERE broadcast_id=? AND ended_at IS NULL',(current['id'],)).fetchone():
                self.gap(current['id'],stamp,None,'collection',snap.get('error') or '未取得有效平台数据')
        self.db.commit()
        return current['id'] if current else None

    def finish(self, sid, stamp, reason):
        self.db.execute("UPDATE broadcasts SET ended_at=?,state='ended',stop_reason=? WHERE id=?",(stamp,reason,sid))
        self.db.execute('UPDATE archive_gaps SET ended_at=? WHERE broadcast_id=? AND ended_at IS NULL',(stamp,sid))

    def gap(self, sid, begin, end, kind, reason):
        self.db.execute('INSERT INTO archive_gaps(broadcast_id,started_at,ended_at,kind,reason) VALUES(?,?,?,?,?)',(sid,begin,end,kind,reason))
        self.db.commit()

    def get(self, sid):
        if sid.startswith('history-'):
            parts=[self.get(i) for i in self.member_ids(sid)]
            result=dict(parts[0])
            result.update(id=sid,first_seen=min(p['first_seen'] for p in parts),last_seen=max(p['last_seen'] for p in parts),
                          ended_at=max(p['ended_at'] or p['last_seen'] for p in parts),title='当日历史汇总',members=parts,fragment_count=len(parts))
            return result
        row=self.db.execute('SELECT * FROM broadcasts WHERE id=?',(sid,)).fetchone()
        if not row: raise KeyError(sid)
        return dict(row)

    def member_ids(self,sid):
        if not sid.startswith('history-'):return [sid]
        match=re.fullmatch(r'history-([a-zA-Z0-9_-]+)-(\d{8})',sid)
        if not match:raise KeyError(sid)
        rid,day=match.groups()
        rows=self.db.execute('SELECT id,first_seen FROM broadcasts WHERE room_id=? AND legacy=1 ORDER BY first_seen',(rid,)).fetchall()
        ids=[r['id'] for r in rows if dt(r['first_seen']).astimezone().strftime('%Y%m%d')==day]
        if not ids:raise KeyError(sid)
        return ids

    def list(self, rid=None, grouped=False):
        query='SELECT b.*, (SELECT COUNT(*) FROM speech_chunks s WHERE s.broadcast_id=b.id AND s.status=\'done\') transcript_count, (SELECT COUNT(*) FROM media_assets m WHERE m.broadcast_id=b.id) video_count FROM broadcasts b'
        rows=[dict(r) for r in self.db.execute(query+(' WHERE b.room_id=?' if rid else '')+' ORDER BY first_seen DESC LIMIT 300',(rid,) if rid else ())]
        if not grouped:return rows
        result={}
        for row in rows:
            key='history-'+row['room_id']+'-'+dt(row['first_seen']).astimezone().strftime('%Y%m%d') if row['legacy'] else row['id']
            if key not in result:
                result[key]=dict(row,id=key,fragment_count=1)
            else:
                group=result[key];group['first_seen']=min(group['first_seen'],row['first_seen']);group['last_seen']=max(group['last_seen'],row['last_seen'])
                group['transcript_count']+=row['transcript_count'];group['video_count']+=row['video_count'];group['fragment_count']+=1
        return sorted(result.values(),key=lambda r:r['first_seen'],reverse=True)

    def speech(self, sid, query=''):
        if sid.startswith('history-'):
            return sorted([r for i in self.member_ids(sid) for r in self.speech(i,query)],key=lambda r:(r['captured_at'],r['id']))
        self.get(sid)
        rows = self.db.execute("SELECT id,captured_at,start_seconds,end_seconds,status,text,segments FROM speech_chunks WHERE broadcast_id=? ORDER BY captured_at,id",(sid,)).fetchall()
        assets=[dict(r) for r in self.db.execute("SELECT id,captured_at,duration,state,mp4_path FROM media_assets WHERE broadcast_id=? ORDER BY captured_at",(sid,))]
        output=[]
        for row in rows:
            r=dict(row)
            if query and query.casefold() not in r['text'].casefold(): continue
            r['match_offset']=0
            if query:
                for part in json.loads(r.pop('segments') or '[]'):
                    if query.casefold() in part['text'].casefold():
                        r['match_offset']=part['start']; break
            else: r.pop('segments')
            target=dt(r['captured_at'])+timedelta(seconds=r['match_offset'])
            r['video']=None
            for asset in assets:
                offset=(target-dt(asset['captured_at'])).total_seconds()
                if 0<=offset<asset['duration'] and asset['state']=='ready' and asset['mp4_path']:
                    r['video']={'id':asset['id'],'offset':round(offset,2)};break
            output.append(r)
        return output

    def detail(self, sid):
        result=self.get(sid)
        if sid.startswith('history-'):
            parts=[self.detail(i) for i in self.member_ids(sid)]
            media=sorted([m for p in parts for m in p['media']],key=lambda m:m['captured_at'])
            points=sorted([point for p in parts for point in p['points']],key=lambda p:p['observed_at'])
            gaps=[g for p in parts for g in p['gaps']]
            end=None
            for part in sorted(parts,key=lambda p:p['first_seen']):
                if end and (dt(part['first_seen'])-dt(end)).total_seconds()>5:
                    gaps.append({'started_at':end,'ended_at':part['first_seen'],'kind':'capture_gap','reason':'两次采音之间未采集的时段'})
                end=max(end or part['last_seen'],part['last_seen'])
            valid=[p for p in points if p.get('status')=='live' and p.get('online') is not None]
            result.update(media=media,points=points,gaps=gaps,peak=max(valid,key=lambda p:p['online']) if valid else None,
                          video_seconds=sum(p['video_seconds'] for p in parts),speech_seconds=sum(p['speech_seconds'] for p in parts),
                          observed_average_online=round(sum(p['online'] for p in valid)/len(valid),1) if valid else None)
            return result
        media=[]
        for row in self.db.execute('SELECT * FROM media_assets WHERE broadcast_id=? ORDER BY captured_at',(sid,)):
            item=dict(row)
            # Paths are not URLs and never accepted from the browser for file reads.
            item['playable']=item['state']=='ready' and bool(item['mp4_path'])
            item['filename']=__import__('pathlib').Path(item['mp4_path'] or item['path']).name
            item.pop('path');item.pop('mp4_path');media.append(item)
        points=[json.loads(r[0]) for r in self.db.execute('SELECT payload FROM observations WHERE broadcast_id=? ORDER BY observed_at,id',(sid,))]
        valid=[p for p in points if p.get('status')=='live' and p.get('online') is not None]
        peak=max(valid,key=lambda p:p['online']) if valid else None
        result.update(media=media, points=points, gaps=[dict(r) for r in self.db.execute('SELECT * FROM archive_gaps WHERE broadcast_id=? ORDER BY started_at',(sid,))],
            peak=peak, video_seconds=round(sum(m['duration'] for m in media),1),
            speech_seconds=self.db.execute("SELECT COALESCE(SUM(end_seconds-start_seconds),0) FROM speech_chunks WHERE broadcast_id=?",(sid,)).fetchone()[0],
            observed_average_online=round(sum(p['online'] for p in valid)/len(valid),1) if valid else None)
        return result

    def analyze(self, sid, save=True):
        info=self.detail(sid); rows=self.speech(sid)
        rules={
            '开场':['欢迎来到','欢迎大家','刚进来','刚进直播间','新进来','晚上好','早上好','下午好'],
            '互动':['公屏','评论区','打个','扣个','扣1','扣一','点个赞','点赞','点关注','大家觉得','告诉我'],
            '转化':['报名','下单','购买','小风车','领资料','领取资料','私信','加微信','加老师','课程价格','学费','名额','优惠','咨询老师'],
            '内容讲解':['考试','备考','岗位','公务员','事业单位','专业','题目','分数','复习','知识点','方法','因为','比如','首先'],
        }
        timeline=[];counts={k:0 for k in [*rules,'待人工判断']};repeated={}
        for row in rows:
            if row['status']!='done':continue
            matches={k:[word for word in words if word in row['text']] for k,words in rules.items()}
            active=[k for k in rules if matches[k]]
            primary=max(active,key=lambda k:(len(matches[k]),k=='转化')) if active else '待人工判断'
            duration=max(0,row['end_seconds']-row['start_seconds']);counts[primary]+=duration
            item={'speech_id':row['id'],'captured_at':row['captured_at'],'seconds':round(duration,1),'category':primary,
                'labels':active,'evidence_terms':matches.get(primary,[]),'quote':row['text'],'video':row['video']}
            timeline.append(item)
            for sentence in re.split(r'[。！？!?\n]',row['text']):
                clean=re.sub(r'[^\w\u4e00-\u9fff]','',sentence)
                if len(clean)>=10:repeated.setdefault(clean,[]).append({'speech_id':row['id'],'captured_at':row['captured_at'],'quote':sentence.strip()})
        rounds=[];last=None
        for item in timeline:
            if item['category']=='转化' and (last is None or (dt(item['captured_at'])-dt(last)).total_seconds()>120):
                rounds.append({'captured_at':item['captured_at'],'speech_id':item['speech_id'],'quote':item['quote']})
            if item['category']=='转化':last=item['captured_at']
        result={'method':'本地规则辅助拆解','notice':'根据转写中的可核对词句标记结构，允许多标签；不确定片段保留待人工判断。轮次为转化片段间隔超过两分钟的候选边界，需要结合录像复核。人数变化只表示同时发生，不证明话术带来转化。',
            'timeline':timeline,'seconds_by_category':{k:round(v,1) for k,v in counts.items()},
            'repeated_passages':[{'text':k,'occurrences':v} for k,v in repeated.items() if len({i['speech_id'] for i in v})>1],
            'conversion_round_candidates':rounds,'peak':info['peak'],'created_at':now(),
            'limitations':['仅分析已采到并完成识别的内容；中断与未开启时段不补写。','视频定位使用本机采集时间，重连与缓存可能造成偏差；可用播放器微调。']}
        key=self.source_key(sid)
        if save:
            self.db.execute('INSERT INTO archive_analyses VALUES(?,?,?,?) ON CONFLICT(broadcast_id) DO UPDATE SET source_key=excluded.source_key,result=excluded.result,created_at=excluded.created_at',
                (sid,key,json.dumps(result,ensure_ascii=False),now()));self.db.commit()
        return result
