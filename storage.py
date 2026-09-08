"""Persistent user preferences, transcript filtering and explicit folder selection."""
import asyncio
import base64
from datetime import datetime, timedelta
import os
from pathlib import Path
import re
import subprocess
import tempfile


def validate_folder(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('首次录制前必须选择视频保存文件夹。')
    folder = Path(value.strip()).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        raise ValueError('请选择已存在的文件夹，并填写完整路径。')
    folder = folder.resolve()
    try:
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError as exc:
        raise ValueError('这个文件夹无法写入，请选择其他位置。') from exc
    return str(folder)


def transcript_filter(rid, day=None):
    sql, params = 'room_id=?', [rid]
    if day:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
            raise ValueError('日期格式应为 YYYY-MM-DD')
        start = datetime.strptime(day, '%Y-%m-%d')
        from datetime import timezone
        sql += ' AND captured_at>=? AND captured_at<?'
        params += [d.astimezone().astimezone(timezone.utc).isoformat() for d in (start, start + timedelta(days=1))]
    return sql, params


async def choose_folder():
    if os.name != 'nt':
        raise ValueError('当前系统请直接填写文件夹完整路径。')
    # No user text is interpolated into executable PowerShell code.
    code = """
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择直播视频保存文件夹'
$dialog.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
try {
  if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write($dialog.SelectedPath)
  }
} finally { $dialog.Dispose(); $owner.Dispose() }
"""
    encoded = base64.b64encode(code.encode('utf-16le')).decode('ascii')
    process = await asyncio.create_subprocess_exec('powershell.exe', '-NoProfile', '-STA', '-WindowStyle', 'Hidden',
        '-EncodedCommand', encoded, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=180)
        if process.returncode:
            raise ValueError('系统文件夹窗口未能打开，请直接填写文件夹完整路径。')
        return out.decode('utf-8-sig').strip()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def list_folders(value=None):
    """Browse local directories in the web UI; no hidden native dialog."""
    roots=[]
    if os.name=='nt':
        import ctypes
        kernel=ctypes.windll.kernel32
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            drive=f'{letter}:\\'
            if kernel.GetDriveTypeW(drive)==3:
                roots.append({'name':f'{letter}:','path':drive})
    else:
        roots=[{'name':'主目录','path':str(Path.home())},{'name':'/','path':'/'}]
    if not value:
        return {'path':None,'parent':None,'directories':roots,'roots':roots}
    if not isinstance(value,str) or len(value)>4096:
        raise ValueError('文件夹路径无效')
    folder=Path(value).expanduser()
    if not folder.is_absolute() or value.startswith(('\\\\','//')):
        raise ValueError('请选择本机磁盘中的完整文件夹路径')
    if os.name=='nt' and kernel.GetDriveTypeW(folder.anchor)!=3:
        raise ValueError('目录浏览目前支持本机固定磁盘；其他位置请直接填写路径')
    try:
        folder=folder.resolve(strict=True)
        if not folder.is_dir():raise ValueError('这个位置不是文件夹')
        directories=[]
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):continue
                    stat=entry.stat(follow_symlinks=False)
                    if getattr(stat,'st_file_attributes',0)&(0x2|0x4|0x400):continue
                    directories.append({'name':entry.name,'path':str(folder/entry.name)})
                except OSError:continue
        directories.sort(key=lambda item:item['name'].casefold())
    except OSError as exc:
        raise ValueError('无法访问此文件夹，请返回上一级或选择其他位置') from exc
    parent=str(folder.parent) if folder.parent!=folder else None
    return {'path':str(folder),'parent':parent,'directories':directories,'roots':roots}
