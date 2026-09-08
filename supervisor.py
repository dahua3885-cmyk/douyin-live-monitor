"""User-session supervisor. Scheduled Task owns this process, not a Codex turn."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from process_job import ProcessJob


def health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
            info = json.load(response)
        return info.get('app') == 'dahua-live-monitor' and info.get('healthy', True)
    except Exception:
        return False


def kill_owned(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def run(config):
    data = Path(config['data_dir'])
    data.mkdir(parents=True, exist_ok=True)
    lock = (data / 'supervisor.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close()
            return 73
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
        RotatingFileHandler(data/'supervisor.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')])
    logging.info('Supervisor started pid=%s', os.getpid())
    (data/'supervisor.pid').write_text(str(os.getpid()), encoding='ascii')
    env = os.environ.copy()
    env.update(config.get('environment', {}))
    env['PYTHONIOENCODING'] = 'utf-8'
    stop = data/'supervisor.stop'
    failures, process, job = 0, None, None
    port = int(config['port'])
    try:
        while not stop.exists():
            if process is None and health(port):
                # Never terminate a service not launched by this supervisor.
                time.sleep(5)
                continue
            if process is None:
                # Preserve earlier output on each restart; service.log is rotated separately.
                out = (data/'server.stdout.log').open('ab')
                err = (data/'server.stderr.log').open('ab')
                try:
                    process = subprocess.Popen([config['python'], '-B', '-u', str(Path(config['app_dir'])/'server.py'),
                        '--data-dir', str(data), '--port', str(port)], cwd=config['app_dir'], env=env,
                        stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    job = ProcessJob(process)
                finally:
                    out.close(); err.close()
                (data/'server.pid').write_text(str(process.pid), encoding='ascii')
                logging.info('Server started pid=%s', process.pid)
                began, unhealthy = time.monotonic(), 0
            time.sleep(5)
            if process.poll() is not None:
                logging.error('Server exited pid=%s code=%s uptime=%.1fs', process.pid, process.returncode, time.monotonic()-began)
                if job:
                    job.close()
                    job = None
                process = None
                failures += 1
                time.sleep(min(30, failures*2))
                continue
            if health(port):
                unhealthy = 0
                if time.monotonic()-began > 60:
                    failures = 0
            else:
                unhealthy += 1
                if unhealthy >= 12:
                    logging.error('Owned server unresponsive for at least 60 seconds; restarting pid=%s', process.pid)
                    kill_owned(process)
                    if job:
                        job.close()
                        job = None
                    process = None
    except BaseException:
        logging.exception('Supervisor stopped unexpectedly')
        raise
    finally:
        if process is not None and process.poll() is None:
            try:
                req = urllib.request.Request(f'http://127.0.0.1:{port}/api/shutdown', data=b'{}', headers={'Content-Type':'application/json'})
                urllib.request.urlopen(req, timeout=5).close()
                process.wait(timeout=30)
            except Exception:
                kill_owned(process)
        logging.info('Supervisor stopped')
        if job:
            job.close()
        lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    sys.exit(run(json.loads(Path(args.config).read_text(encoding='utf-8-sig'))))
