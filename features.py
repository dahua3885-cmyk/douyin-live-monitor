"""Archive, batch collection and user-configured notification HTTP surfaces."""
import asyncio
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import re
from aiohttp import web
from storage import validate_folder
from recording_locations import recording_location,open_directory


def import_rows(body):
    value=body.get('text','')
    if not isinstance(value,str) or len(value)>200000:raise ValueError('请粘贴不超过 200 KB 的主播清单')
    stripped=value.lstrip('\ufeff').strip()
    if not stripped:return []
    if stripped[0] in '[{':
        data=json.loads(stripped)
        rows=data.get('rooms',[]) if isinstance(data,dict) else data
        if not isinstance(rows,list):raise ValueError('JSON 清单应为 rooms 数组')
        return [{'url':r.get('url'),'name':r.get('name',''),'group_name':r.get('group_name','')} for r in rows if isinstance(r,dict)]
    lines=stripped.splitlines()
    if any('url' in part.lower() for part in next(csv.reader([lines[0]]))):
        return [dict(r) for r in csv.DictReader(io.StringIO(stripped))]
    rows=[]
    for line in lines:
        if not line.strip():continue
        match=re.search(r'https?://[^\s<>"，,；;）)]+',line)
        url=match.group(0) if match else line.strip()
        name=line[:match.start()].strip(' \t,，') if match else ''
        if '\t' in line and not name:name=line.split('\t',1)[-1].strip()
        rows.append({'url':url,'name':name,'group_name':body.get('group_name','')})
    return rows


def register_features(app,store,archive,media,recorder,speech,monitor,notifier,folder_lock):
    from server import normalize_url,utcnow

    async def bulk_import(request):
        body=await request.json()
        if not isinstance(body,dict):raise ValueError('请填写主播清单')
        rows=import_rows(body)
        if len(rows)>200:raise ValueError('每次最多导入 200 行')
        added=[];duplicates=[];errors=[];seen=set()
        for number,row in enumerate(rows,1):
            try:
                url=normalize_url(row.get('url'))
                if url in seen or store.db.execute('SELECT 1 FROM rooms WHERE url=? AND deleted=0',(url,)).fetchone():
                    duplicates.append({'line':number,'url':url});continue
                seen.add(url)
                room=store.add(url,row.get('name',''))
                if row.get('group_name'):room=store.update(room['id'],{'group_name':row['group_name']})
                monitor.queue(room['id']);added.append(room)
            except ValueError as exc:errors.append({'line':number,'error':str(exc)})
        return web.json_response({'added':added,'duplicates':duplicates,'errors':errors})

    async def export_rooms(request):
        data={'version':1,'rooms':[{k:r.get(k) for k in ['url','name','group_name']} for r in store.rooms()]}
        return web.Response(body=json.dumps(data,ensure_ascii=False,indent=2).encode(),content_type='application/json',headers={'Content-Disposition':'attachment; filename="live-rooms.json"'})

    async def bulk_update(request):
        body=await request.json()
        if not isinstance(body,dict):raise ValueError('批量设置格式无效')
        ids=body.get('ids');changes=body.get('changes')
        if not isinstance(ids,list) or not ids or len(ids)>200 or any(not isinstance(r,str) for r in ids):raise ValueError('请先勾选直播间')
        ids=list(dict.fromkeys(ids))
        if not isinstance(changes,dict) or 'name' in changes:raise ValueError('批量设置不支持统一改名')
        async with folder_lock:
            for rid in ids:store.update(rid,changes,validate_only=True)
            if changes.get('record_enabled'):await asyncio.to_thread(validate_folder,store.setting('recording_dir'))
            try:
                rooms=[store.update(rid,changes,commit=False) for rid in ids];store.db.commit()
            except Exception:store.db.rollback();raise
        for room in rooms:
            rid=room['id']
            if not room['enabled']:
                monitor.forced.discard(rid)
                active=archive.current(rid)
                if active:archive.gap(active['id'],utcnow(),None,'pause','批量暂停监控')
            if not room['enabled'] or not room['record_enabled']:await recorder.stop(rid)
            if not room['enabled'] or not room['transcribe_enabled']:await speech.stop(rid)
            if room['enabled']:monitor.queue(rid)
        return web.json_response({'updated':len(rooms)})

    async def archives(request):
        return web.json_response({'items':archive.list(request.query.get('room_id'),grouped=True)})

    async def bulk_remove(request):
        body=await request.json()
        ids=body.get('ids') if isinstance(body,dict) else None
        if not isinstance(ids,list) or not ids or len(ids)>200 or any(not isinstance(i,str) for i in ids):raise ValueError('请先勾选要移除的账号')
        ids=list(dict.fromkeys(ids))
        for rid in ids:
            if not store.get(rid):raise KeyError(rid)
        for rid in ids:
            store.update(rid,{'enabled':False,'record_enabled':False,'transcribe_enabled':False},commit=False)
            monitor.forced.discard(rid)
        store.db.commit()
        await asyncio.gather(*(recorder.stop(rid) for rid in ids),*(speech.stop(rid) for rid in ids))
        speech.scan()
        for rid in ids:
            active=archive.current(rid)
            if active:archive.gap(active['id'],utcnow(),None,'pause','批量移出采集列表，历史资料保留')
            store.delete(rid)
        return web.json_response({'removed':len(ids)})

    async def detail(request):return web.json_response(archive.detail(request.match_info['sid']))

    async def search(request):
        query=request.query.get('q','')
        if len(query)>100:raise ValueError('搜索词最多 100 个字符')
        return web.json_response({'items':archive.speech(request.match_info['sid'],query)})

    async def analyze(request):return web.json_response(archive.analyze(request.match_info['sid']))

    async def analysis_export(request):
        sid=request.match_info['sid'];result=archive.analyze(sid,save=False);info=archive.get(sid)
        lines=[f"# {info['name']}｜直播结构拆解",'',result['method'],result['notice'],'',f"采集开始：{info['first_seen']}",'']
        for item in result['timeline']:
            stamp=datetime.fromisoformat(item['captured_at']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            lines.extend([f"## {stamp}｜{item['category']}",f"证据词：{'、'.join(item['evidence_terms']) or '不足，需人工判断'}",item['quote'],''])
        lines+=['## 重复话术']
        for item in result['repeated_passages']:lines+=[f"{len(item['occurrences'])} 次：{item['text']}"]
        return web.Response(body=('\ufeff'+'\n\n'.join(lines)).encode('utf-8'),content_type='text/plain',headers={'Content-Disposition':f'attachment; filename="analysis-{sid}.md"'})

    async def media_file(request):
        row=store.db.execute('SELECT * FROM media_assets WHERE id=?',(request.match_info['mid'],)).fetchone()
        if not row:raise KeyError('media')
        original=request.query.get('original')=='1'
        value=row['path'] if original else row['mp4_path']
        if not value or not Path(value).is_file():raise ValueError('文件尚未准备好或已被移动')
        headers={}
        if original or request.query.get('download')=='1':headers['Content-Disposition']=f'attachment; filename="video-{row["id"]}{Path(value).suffix}"'
        return web.FileResponse(Path(value),headers=headers)

    async def audio_file(request):
        row=store.db.execute('SELECT audio_path FROM speech_chunks WHERE id=?',(request.match_info['cid'],)).fetchone()
        if not row or not Path(row[0]).is_file():raise KeyError('audio')
        return web.FileResponse(Path(row[0]))

    async def retry_media(request):
        row=store.db.execute('SELECT id FROM media_assets WHERE id=?',(request.match_info['mid'],)).fetchone()
        if not row:raise KeyError('media')
        store.db.execute("UPDATE media_assets SET state='pending',error=NULL,convert_enabled=1 WHERE id=? AND state NOT IN ('ready','converting')",(row[0],));store.db.commit()
        return web.json_response({'queued':True})

    async def open_recordings(request):
        location=recording_location(store,recorder,room_id=request.match_info.get('rid'),media_id=request.match_info.get('mid'))
        path=await asyncio.to_thread(open_directory,location)
        return web.json_response({'opened':True,'path':path})

    async def push_get(request):return web.json_response(notifier.public())
    async def push_save(request):return web.json_response(notifier.save(await request.json()))
    async def push_test(request):
        await notifier.send('直播监控｜测试消息\n群机器人连接已成功。')
        return web.json_response({'sent':True})

    app.router.add_post('/api/rooms/import',bulk_import)
    app.router.add_post('/api/rooms/bulk',bulk_update)
    app.router.add_post('/api/rooms/bulk-remove',bulk_remove)
    app.router.add_get('/api/rooms/export',export_rooms)
    app.router.add_get('/api/archives',archives)
    app.router.add_get('/api/archives/{sid}',detail)
    app.router.add_get('/api/archives/{sid}/speech',search)
    app.router.add_post('/api/archives/{sid}/analyze',analyze)
    app.router.add_get('/api/archives/{sid}/analysis.md',analysis_export)
    app.router.add_get('/api/media/{mid}',media_file)
    app.router.add_post('/api/media/{mid}/retry',retry_media)
    app.router.add_post('/api/media/{mid}/open-folder',open_recordings)
    app.router.add_post('/api/rooms/{rid}/open-recordings',open_recordings)
    app.router.add_post('/api/recordings/open-folder',open_recordings)
    app.router.add_get('/api/speech/{cid}/audio',audio_file)
    app.router.add_get('/api/push',push_get)
    app.router.add_post('/api/push',push_save)
    app.router.add_post('/api/push/test',push_test)
