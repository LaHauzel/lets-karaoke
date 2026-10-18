"""Bounded per-song workers release CUDA/WDDM allocations between songs."""
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def run_worker(command,progress_file,idle_timeout=180,poll_interval=2):
    progress_file=Path(progress_file)
    process=subprocess.Popen(command,start_new_session=os.name!='nt')
    last_progress=time.monotonic();signature=None
    try:
        while process.poll() is None:
            try:
                stat=progress_file.stat();current=(stat.st_mtime_ns,stat.st_size)
                if current!=signature:signature=current;last_progress=time.monotonic()
            except FileNotFoundError:pass
            if time.monotonic()-last_progress>idle_timeout:
                try:phase=json.loads(progress_file.read_text(encoding='utf8'))
                except (OSError,ValueError):phase={}
                raise TimeoutError('声学计算长时间无进度：'+json.dumps(phase,ensure_ascii=False))
            time.sleep(poll_interval)
        if process.returncode:raise subprocess.CalledProcessError(process.returncode,command)
    finally:
        if process.poll() is None:
            if os.name=='nt':
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            else:os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=30)
