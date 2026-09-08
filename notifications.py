"""Opt-in Feishu webhook delivery with bounded retries and durable deduplication."""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import urlsplit
from datetime import datetime, timezone
import aiohttp

KINDS={'live':'主播开播','offline':'主播下播','recording':'开始录制','record_error':'录制异常',
       'record_stopped':'停止录制','record_limit':'到达时长上限','disk_warning':'磁盘空间不足',
       'archive_ready':'整场资料归档完成','media_ready':'录像片段归档完成','media_error':'录像转换失败','above':'在线人数高于阈值','below':'在线人数低于阈值'}
DEFAULT_KINDS=['live','record_error','disk_warning','archive_ready','media_error']


def validate_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!='open.feishu.cn' or not re.fullmatch(r'/open-apis/bot/v2/hook/[A-Za-z0-9_-]{10,160}',p.path) or p.query or p.fragment:
        raise ValueError('请填写飞书自定义机器人的 HTTPS Webhook 地址。')
    return url


def payload(text,secret='',timestamp=None):
    result={'msg_type':'text','content':{'text':text[:4000]}}
    if secret:
        stamp=int(timestamp if timestamp is not None else time.time())
        key=f'{stamp}\n{secret}'.encode()
        result.update(timestamp=str(stamp),sign=base64.b64encode(hmac.new(key,b'',hashlib.sha256).digest()).decode())
    return result


class Notifications:
    def __init__(self,store):
        self.store,self.db=store,store.db;self.task=None;self.closed=False
        self.db.execute('CREATE TABLE IF NOT EXISTS push_deliveries (event_id INTEGER PRIMARY KEY,status TEXT,attempts INTEGER DEFAULT 0,next_at REAL DEFAULT 0,error TEXT,updated_at TEXT)')
        self.db.execute("UPDATE push_deliveries SET status='pending' WHERE status='sending'");self.db.commit()

    def config(self):
        return json.loads(self.store.setting('feishu_push') or '{}')

    def public(self):
        c=self.config()
        return {'enabled':c.get('enabled',False),'configured':bool(c.get('webhook')),'has_secret':bool(c.get('secret')),
                'events':c.get('events',DEFAULT_KINDS),'labels':KINDS,
                'logs':[dict(r) for r in self.db.execute('SELECT d.event_id,d.status,d.attempts,d.error,d.updated_at,e.kind FROM push_deliveries d LEFT JOIN events e ON e.id=d.event_id ORDER BY event_id DESC LIMIT 30')]}

    def save(self,body):
        if not isinstance(body,dict) or type(body.get('enabled')) is not bool:raise ValueError('请选择是否开启飞书推送')
        c=self.config();url=body.get('webhook') or c.get('webhook','')
        events=body.get('events',DEFAULT_KINDS)
        if not isinstance(events,list) or any(e not in KINDS for e in events):raise ValueError('推送事件设置无效')
        if url:validate_url(url)
        if body['enabled'] and not url:raise ValueError('先填写飞书群机器人的 Webhook 地址')
        secret=body.get('secret') or c.get('secret','')
        if body.get('clear_secret'):secret=''
        if not isinstance(secret,str) or len(secret)>300:raise ValueError('签名密钥格式无效')
        maximum=self.db.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        c={'enabled':body['enabled'],'webhook':url,'secret':secret,'events':events,'after_id':maximum}
        self.db.execute("UPDATE push_deliveries SET status='cancelled' WHERE status='pending'")
        self.store.set_setting('feishu_push',json.dumps(c,ensure_ascii=False))
        return self.public()

    async def send(self,text,config=None):
        c=config or self.config();url=validate_url(c.get('webhook',''))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
                async with client.post(url,json=payload(text,c.get('secret','')),allow_redirects=False) as response:
                    data=await response.json(content_type=None)
                    if response.status!=200 or data.get('code',data.get('StatusCode',-1))!=0:
                        raise ValueError('飞书拒绝了推送，请检查机器人关键词、签名密钥或权限设置。')
        except (aiohttp.ClientError,asyncio.TimeoutError):
            raise ValueError('无法连接飞书，稍后会重试。') from None

    async def run(self):
        while not self.closed:
            c=self.config()
            if not c.get('enabled'):
                await asyncio.sleep(2);continue
            events=self.db.execute('SELECT id,kind,created_at FROM events WHERE id>? ORDER BY id LIMIT 100',(c.get('after_id',0),)).fetchall()
            for event in events:
                age=time.time()-datetime.fromisoformat(event['created_at']).timestamp()
                if event['kind'] in c.get('events',[]) and age<7200:
                    self.db.execute("INSERT OR IGNORE INTO push_deliveries(event_id,status) VALUES(?,'pending')",(event['id'],))
                c['after_id']=event['id']
            if events:self.store.set_setting('feishu_push',json.dumps(c,ensure_ascii=False))
            row=self.db.execute("SELECT d.*,e.kind,e.room_id,e.message,e.created_at FROM push_deliveries d JOIN events e ON e.id=d.event_id WHERE d.status='pending' AND d.next_at<=? ORDER BY event_id LIMIT 1",(time.time(),)).fetchone()
            if not row:
                await asyncio.sleep(2);continue
            if time.time()-datetime.fromisoformat(row['created_at']).timestamp()>7200:
                self.db.execute("UPDATE push_deliveries SET status='expired' WHERE event_id=?",(row['event_id'],));self.db.commit();continue
            try:
                self.db.execute("UPDATE push_deliveries SET status='sending' WHERE event_id=?",(row['event_id'],));self.db.commit()
                room=self.store.get(row['room_id']) if row['room_id'] else None
                name=(room['name'] or (room.get('latest') or {}).get('nickname') or '直播间') if room else '直播监控台'
                stamp=datetime.fromisoformat(row['created_at']).astimezone().strftime('%m-%d %H:%M:%S')
                await self.send(f'直播监控｜{KINDS.get(row["kind"],row["kind"])}\n{name}\n{stamp}\n{row["message"]}',c)
                self.db.execute("UPDATE push_deliveries SET status='sent',attempts=attempts+1,error=NULL,updated_at=? WHERE event_id=?",(datetime.now(timezone.utc).isoformat(),row['event_id']))
            except asyncio.CancelledError:
                self.db.execute("UPDATE push_deliveries SET status='pending' WHERE event_id=?",(row['event_id'],));self.db.commit();raise
            except Exception:
                attempts=row['attempts']+1
                self.db.execute('UPDATE push_deliveries SET status=?,attempts=?,next_at=?,error=?,updated_at=? WHERE event_id=?',
                    ('failed' if attempts>=3 else 'pending',attempts,time.time()+30*attempts,'推送未成功，请检查网络和机器人安全设置。',datetime.now(timezone.utc).isoformat(),row['event_id']))
            self.db.commit();await asyncio.sleep(3)

    async def close(self):
        self.closed=True
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
