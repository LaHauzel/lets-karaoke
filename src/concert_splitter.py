"""Bounded-memory concert boundary suggestions and non-destructive FFmpeg export.

Acoustic heuristics only: scores are ranking signals, not song probabilities.
No downloaded models, transcription, or song-name recognition is implied.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d, maximum_filter1d
from scipy.signal import find_peaks

CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class Cancelled(Exception):
    pass


def probe(source):
    p = Path(str(source).strip().strip('"')).expanduser().resolve()
    if not p.is_file() or p.suffix.lower() not in {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v', '.ts', '.mts'}:
        raise ValueError('请选择存在的本地视频文件')
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(p)],
                            capture_output=True, timeout=45, creationflags=CREATE_FLAGS)
    if result.returncode:
        raise ValueError('无法读取视频：' + result.stderr.decode('utf-8', 'replace')[-500:])
    info = json.loads(result.stdout)
    duration = float(info.get('format', {}).get('duration', 0))
    video = next((s for s in info['streams'] if s['codec_type'] == 'video'), None)
    audio = next((s for s in info['streams'] if s['codec_type'] == 'audio'), None)
    if not video or not audio or not math.isfinite(duration) or duration <= 0:
        raise ValueError('视频必须有可读取的时长、画面和音轨')
    return {'source': str(p), 'name': p.name, 'duration': duration, 'size': p.stat().st_size,
            'width': video.get('width'), 'height': video.get('height'),
            'video_codec': video['codec_name'], 'audio_codec': audio['codec_name']}


def extract_features(source, duration, progress=lambda *_: None, cancelled=lambda: False):
    """Decode mono 8 kHz audio one second at a time, retaining only summaries."""
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source), '-map', '0:a:0',
               '-vn', '-ac', '1', '-ar', '8000', '-f', 'f32le', 'pipe:1']
    energies, spectra = [], []
    # Send stderr to a temporary file to avoid an unread-pipe deadlock.
    import tempfile
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, creationflags=CREATE_FLAGS)
        try:
            edges = np.unique(np.geomspace(1, 257, 33).astype(int))
            while True:
                if cancelled():
                    raise Cancelled()
                data = process.stdout.read(8000 * 4)
                if not data:
                    break
                samples = np.frombuffer(data, dtype='<f4')
                energies.append(float(20 * np.log10(np.sqrt(np.mean(samples ** 2)) + 1e-9)))
                padded = np.pad(samples, (0, (-len(samples)) % 512))
                power = np.abs(np.fft.rfft(padded.reshape(-1, 512) * np.hanning(512), axis=1)) ** 2
                bands = np.array([power[:, a:b].mean() for a, b in zip(edges[:-1], edges[1:])])
                spectra.append(np.log1p(bands))
                if len(energies) % 60 == 0:
                    progress(min(.85, .85 * len(energies) / duration), f'已分析 {len(energies)//60} / {math.ceil(duration/60)} 分钟音频')
            process.wait(timeout=30)
            if process.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read().decode('utf-8', 'replace')[-1000:])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
    if len(energies) < min(3, duration * .8) or len(energies) < duration - max(10, duration * .01):
        raise ValueError('音轨解码不完整，无法为整段视频生成可靠时间表')
    return np.asarray(energies), np.asarray(spectra)


def suggest(energy, spectra, duration, min_length=120, sensitivity='balanced'):
    if sensitivity not in {'conservative', 'balanced', 'sensitive'}:
        raise ValueError('无效分析灵敏度')
    min_length = float(min_length)
    if not math.isfinite(min_length) or not 30 <= min_length <= 1800:
        raise ValueError('最短片段应在 30～1800 秒之间')
    smooth = gaussian_filter1d(energy, 2)
    baseline = maximum_filter1d(smooth, size=91, mode='nearest')
    dip = np.clip((baseline - smooth - 3) / 15, 0, 1)
    # Compare sustained spectral context before and after each second.
    normalized = spectra / (np.linalg.norm(spectra, axis=1, keepdims=True) + 1e-9)
    prefix = np.vstack([np.zeros((1, spectra.shape[1])), np.cumsum(normalized, axis=0)])
    t = np.arange(len(energy))
    left, right = np.maximum(0, t-15), np.minimum(len(energy), t+15)
    before = (prefix[t] - prefix[left]) / np.maximum(1, t-left)[:, None]
    after = (prefix[right] - prefix[t]) / np.maximum(1, right-t)[:, None]
    change = np.linalg.norm(before - after, axis=1)
    scale = max(float(np.percentile(change, 95)), .08)
    change = np.clip(gaussian_filter1d(change, 2) / scale, 0, 1)
    score = .68 * dip + .32 * change
    threshold = {'conservative': .65, 'balanced': .48, 'sensitive': .34}[sensitivity]
    peaks, _ = find_peaks(score, height=threshold, distance=15, prominence=.08)
    # Greedy non-maximum suppression, including the video endpoints.
    selected = []
    for i in sorted(peaks, key=lambda i: float(score[i]), reverse=True):
        if i < min_length or duration-i < min_length or any(abs(i-j) < min_length for j in selected):
            continue
        selected.append(int(i))
    candidates = []
    for i in sorted(selected):
        reasons = []
        if dip[i] > .3:
            reasons.append(f'局部音量下降约 {baseline[i]-smooth[i]:.0f} dB')
        if change[i] > .45:
            reasons.append('前后音色结构变化')
        candidates.append({'time': float(i), 'score': round(float(score[i]), 3),
                           'reason': '；'.join(reasons) or '声学变化', 'reviewed': False})
    points = [0.] + [c['time'] for c in candidates] + [duration]
    segments = [{'title': f'片段 {i+1:02d}', 'start': a, 'end': b, 'selected': True}
                for i, (a, b) in enumerate(zip(points[:-1], points[1:]))]
    hop = max(1, math.ceil(len(energy)/2400))
    waveform = [{'time': i, 'db': round(float(np.max(energy[i:i+hop])), 1)} for i in range(0, len(energy), hop)]
    return {'candidates': candidates, 'segments': segments, 'waveform': waveform,
            'method': 'energy-spectral-v1', 'min_length': min_length, 'sensitivity': sensitivity,
            'notice': '声学候选分段，未识别歌名或验证歌曲完整性。请试听边界；讲话、掌声、串烧可能产生误切或漏切。'}


def validate_segments(segments, duration):
    if not isinstance(segments, list) or not 1 <= len(segments) <= 500:
        raise ValueError('请提供 1～500 个片段')
    clean, previous = [], 0.
    for i, item in enumerate(segments):
        a, b = float(item['start']), float(item['end'])
        if not all(map(math.isfinite, (a, b))) or a < 0 or b > duration + .05 or b-a < .2:
            raise ValueError(f'第 {i+1} 段时间无效，需在视频范围内且至少 0.2 秒')
        if a < previous - .001:
            raise ValueError(f'第 {i+1} 段与上一段重叠或顺序错误')
        previous = b
        title = str(item.get('title') or f'片段 {i+1:02d}').strip()[:100]
        clean.append({'start': a, 'end': min(b, duration), 'title': title, 'selected': bool(item.get('selected', True))})
    return clean


def export_segments(source, segments, directory, mode, progress=lambda *_: None, cancelled=lambda: False):
    if mode not in {'copy', 'precise'}:
        raise ValueError('无效导出模式')
    selected = [s for s in segments if s['selected']]
    if not selected:
        raise ValueError('请至少选中一个片段')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    results = []
    total = sum(s['end'] - s['start'] for s in selected)
    completed = 0
    for i, s in enumerate(selected):
        if cancelled():
            raise Cancelled()
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s['title']).strip(' .')[:80] or '片段'
        # Matroska preserves arbitrary source codecs in stream-copy mode.
        target = directory / f'{i+1:02d}_{name}{".mkv" if mode == "copy" else ".mp4"}'
        partial = target.with_name(target.stem + '.partial' + target.suffix)
        command = ['ffmpeg', '-v', 'error', '-nostdin', '-n', '-ss', str(s['start']), '-i', str(source),
                   '-t', str(s['end']-s['start']), '-map', '0:v:0', '-map', '0:a:0', '-sn', '-dn', '-map_chapters', '-1']
        if mode == 'copy':
            command += ['-c', 'copy', '-avoid_negative_ts', 'make_zero']
        else:
            command += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart']
        command += ['-progress', str(directory/'ffmpeg-progress.txt'), '-nostats', str(partial)]
        (directory/'ffmpeg-progress.txt').unlink(missing_ok=True)
        with (directory/'ffmpeg.log').open('ab') as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, creationflags=CREATE_FLAGS)
            try:
                while process.poll() is None:
                    if cancelled():
                        raise Cancelled()
                    fraction = 0
                    try:
                        entries = re.findall(r'out_time_us=(\d+)', (directory/'ffmpeg-progress.txt').read_text())
                        if entries:
                            fraction = min(s['end']-s['start'], int(entries[-1])/1e6)
                    except OSError:
                        pass
                    progress((completed+fraction)/total, f'正在导出 {i+1}/{len(selected)}：{s["title"]}')
                    time.sleep(.4)
                if process.returncode:
                    raise RuntimeError('FFmpeg 导出失败：' + (directory/'ffmpeg.log').read_text(encoding='utf-8', errors='replace')[-900:])
                if cancelled():
                    raise Cancelled()
                partial.rename(target)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                partial.unlink(missing_ok=True)
        results.append({**s, 'file': target.name, 'size': target.stat().st_size})
        completed += s['end']-s['start']
    manifest = {'source': str(source), 'mode': mode, 'segments': results,
                'notice': '快速模式切点受关键帧影响，实际片段可能包含切点前画面。' if mode == 'copy' else '精确重编码导出。'}
    (directory/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return results
