"""Small cached audio envelope for the subtitle editor (no model required)."""
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import threading
import wave

import numpy as np

LOCK = threading.Lock()


def waveform(directory):
    directory = Path(directory)
    job = json.loads((directory/'job.json').read_text(encoding='utf-8'))
    choices = [(directory/'in/vg/vocals.wav', '分离人声'),
               (Path(job.get('align_source') or ''), '对齐音轨'),
               (Path(job['media']), '原始音轨')]
    source, label = next(((p,s) for p,s in choices if p.is_file()), (None,None))
    if source is None:
        raise ValueError('音频已移动或丢失，无法显示波形')
    duration = float(job['media_info']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('媒体时长无效')
    stat = source.stat()
    key = hashlib.sha256(f'{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:v1'.encode()).hexdigest()
    cache = directory/'waveform.json'
    with LOCK:
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                data = {}
            if data.get('key') == key:
                return data
        with tempfile.TemporaryDirectory(prefix='karaoke_wave_') as temp:
            audio = Path(temp)/'wave.wav'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(source),'-vn',
                            '-ar','2000','-ac','1','-c:a','pcm_s16le',str(audio)],
                           check=True, capture_output=True, timeout=180)
            peaks = []
            with wave.open(str(audio), 'rb') as wav:
                hop = max(40, math.ceil(wav.getnframes()/12000))
                while data := wav.readframes(hop):
                    values = np.frombuffer(data, dtype='<i2').astype(np.float32)
                    peaks.append(round(float(np.max(np.abs(values)))/32768, 4))
                step = hop/wav.getframerate()
        result = {'key':key, 'source':label, 'duration':duration, 'step':step, 'peaks':peaks}
        temporary = cache.with_suffix('.tmp')
        temporary.write_text(json.dumps(result), encoding='utf-8')
        temporary.replace(cache)
        return result
