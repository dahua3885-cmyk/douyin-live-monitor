"""Keep completed MP4 folders clean while preserving verified source backups."""
import asyncio
import hashlib
from pathlib import Path
import shutil


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').digest()


def backup_file(source,target):
    if target.exists():
        if digest(source)!=digest(target):raise ValueError('内部备份与原文件不同，保留原文件')
    else:
        temporary=target.with_name(target.name+'.copying')
        shutil.copy2(source,temporary)
        if digest(source)!=digest(temporary):raise ValueError('备份校验失败，保留原文件')
        temporary.replace(target)


def remove_duplicate(file,backup,public):
    if file.resolve().parent==public and digest(file)==digest(backup):file.unlink()


async def clean_ready_runs(media):
    root=(media.store.data_dir/'recording-sources').resolve()
    for raw in media.db.execute('SELECT * FROM recording_runs WHERE closed=1 AND convert_enabled=1').fetchall():
        run=dict(raw);public=Path(run['output_folder'] or run['folder']).resolve()
        source=(root/run['id']).resolve()
        if source==root or not source.is_relative_to(root) or source==public:continue
        files=[p for p in public.glob('part-*.ts') if p.is_file()]
        metadata=[p for p in (public/'recording.json',public/'segments.csv') if p.is_file()]
        if not files and not metadata:continue
        assets=[]
        known={str(Path(row['path']).resolve()):dict(row) for row in media.db.execute('SELECT * FROM media_assets WHERE room_id=? AND broadcast_id=?',(run['room_id'],run['broadcast_id']))}
        for file in files:
            # On interrupted cleanup, the DB can already point to the verified backup.
            row=known.get(str(file)) or known.get(str(source/file.name))
            if not row or row['state']!='ready' or not row['mp4_path'] or not Path(row['mp4_path']).is_file():break
            assets.append(dict(row))
        else:
            if not assets and not run['output_folder']:continue
            try:
                for asset in assets:
                    _,duration=await media.probe(asset['mp4_path'])
                    if abs(duration-asset['duration'])>max(2,asset['duration']*.01):raise ValueError('MP4 时长不匹配，暂不整理原文件')
                source.mkdir(parents=True,exist_ok=True)
                for file in files+metadata:
                    await asyncio.to_thread(backup_file,file,source/file.name)
                # Copies exist and are verified before any path changes are committed.
                for file,asset in zip(files,assets):
                    media.db.execute('UPDATE media_assets SET path=? WHERE id=?',(str(source/file.name),asset['id']))
                media.db.execute('UPDATE recording_runs SET folder=?,output_folder=? WHERE id=?',(str(source),str(public),run['id']))
                media.db.commit()
                for file in files+metadata:
                    # Only remove this known run's verified duplicate, never recurse.
                    await asyncio.to_thread(remove_duplicate,file,source/file.name,public)
                media.scan_stamps.clear()
            except (OSError,ValueError,RuntimeError):
                # Conversion remains usable, and an incomplete cleanup keeps originals.
                media.db.rollback()
