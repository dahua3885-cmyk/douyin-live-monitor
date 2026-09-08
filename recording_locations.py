"""Resolve application-owned recording paths before opening their directories."""
import os
from pathlib import Path


def video_in(folder):
    folder=Path(folder)
    if not folder.is_dir():return None
    files=[p for p in folder.glob('part-*') if p.suffix.lower() in {'.mp4','.ts'} and p.is_file() and p.stat().st_size>0]
    return max(files,key=lambda p:(p.stat().st_mtime_ns,p.suffix=='.mp4')) if files else None


def recording_location(store,recorder,room_id=None,media_id=None):
    if media_id:
        row=store.db.execute('SELECT path,mp4_path FROM media_assets WHERE id=?',(media_id,)).fetchone()
        if not row:raise KeyError('media')
        for value in (row['mp4_path'],row['path']):
            if value and Path(value).is_file():return Path(value).resolve().parent
        raise ValueError('录像文件已被移动或删除，请检查原保存目录')
    if not room_id:
        value=store.setting('recording_dir')
        if not value:raise ValueError('请先设置视频保存文件夹')
        return Path(value).resolve()
    if not store.get(room_id):raise KeyError('room')
    run=getattr(recorder,'runs',{}).get(room_id)
    if run:
        file=video_in(run['folder'])
        if file:return file.resolve().parent
    # Runs cover actively written TS files that have not entered the archive yet.
    locations=[(row['started_at'],row['folder'],True) for row in store.db.execute('SELECT started_at,COALESCE(output_folder,folder) AS folder FROM recording_runs WHERE room_id=? ORDER BY started_at DESC LIMIT 100',(room_id,))]
    for row in store.db.execute('SELECT captured_at,path,mp4_path FROM media_assets WHERE room_id=? ORDER BY captured_at DESC LIMIT 100',(room_id,)):
        locations.extend((row['captured_at'],value,False) for value in (row['mp4_path'],row['path']) if value)
    for _,value,is_folder in sorted(locations,key=lambda item:item[0],reverse=True):
        file=video_in(value) if is_folder else Path(value)
        if file and file.is_file() and file.stat().st_size>0:return file.resolve().parent
    raise ValueError('这个账号还没有已写入的视频文件。请先开启录像，并等待收到直播画面。')


def open_directory(folder):
    folder=Path(folder).resolve()
    if not folder.is_dir():raise ValueError('录像文件夹不存在或已被移动')
    if os.name!='nt':raise ValueError(f'请在文件管理器中打开：{folder}')
    # Only a directory resolved from saved application records is opened.
    # Never pass an executable or user-supplied command to the shell.
    import json
    import subprocess
    script=Path(__file__).with_name('open-recording-folder.ps1')
    result=subprocess.run(['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',str(script),'-Folder',str(folder)],capture_output=True,text=True,encoding='utf-8-sig',timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise ValueError('无法打开录像文件夹，请稍后重试')
    info=json.loads(result.stdout)
    return {'path':str(folder),'foreground':bool(info.get('foreground'))}
