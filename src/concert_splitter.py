"""Bounded-memory concert boundary suggestions and non-destructive FFmpeg export.

Song boundaries use acoustic features; YAMNet and WebRTC VAD supply speech/music
review markers. Neither score is calibrated for live concert audio.
"""
from __future__ import annotations

import json
import hashlib
import math
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter1d, maximum_filter1d
from scipy.signal import find_peaks
from process_runner import attach_kill_on_close_job, close_kill_on_close_job

CREATE_FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
DECODE_IDLE_TIMEOUT = 60.
EXPORT_IDLE_TIMEOUT = 300.
PROCESSES = set()
PROCESS_LOCK = threading.RLock()


class Cancelled(Exception):
    pass


def source_identity(source):
    """A cheap file identity: metadata plus samples from beginning/middle/end."""
    path = Path(source)
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for offset in sorted({0, max(0, stat.st_size // 2 - 32768), max(0, stat.st_size - 65536)}):
            stream.seek(offset)
            digest.update(stream.read(65536))
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('原视频正在改变，请等待文件写入完成后重试')
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns, 'sample_sha256': digest.hexdigest()}


def verify_source(source, expected):
    current = source_identity(source)
    if expected and current != expected:
        raise ValueError('原视频已被替换或修改，请为当前文件重新建立分析记录')
    return current


def _launch(command, **kwargs):
    process = subprocess.Popen(command, creationflags=CREATE_FLAGS, **kwargs)
    attach_kill_on_close_job(process)
    with PROCESS_LOCK:
        PROCESSES.add(process)
    return process


def _stop_process(process):
    try:
        close_kill_on_close_job(process)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    finally:
        close_kill_on_close_job(process)
        with PROCESS_LOCK:
            PROCESSES.discard(process)


def cancel_running_processes():
    with PROCESS_LOCK:
        for process in list(PROCESSES):
            close_kill_on_close_job(process)
            if process.poll() is None:
                process.kill()


def _decoded_chunks(command, chunk_bytes, duration, cancelled):
    """Read a pipe on a bounded reader thread so cancellation never waits on read."""
    import tempfile
    pending, stop = queue.Queue(maxsize=2), threading.Event()
    with tempfile.TemporaryFile() as errors:
        process = _launch(command, stdout=subprocess.PIPE, stderr=errors)
        def reader():
            try:
                while not stop.is_set():
                    chunk = process.stdout.read(chunk_bytes)
                    while not stop.is_set():
                        try:
                            pending.put(chunk, timeout=.1)
                            break
                        except queue.Full:
                            pass
                    if not chunk:
                        break
            except (OSError, ValueError) as exc:
                while not stop.is_set():
                    try:
                        pending.put(exc, timeout=.1)
                        break
                    except queue.Full:
                        pass
        worker = threading.Thread(target=reader, daemon=True)
        worker.start()
        started = last_data = time.monotonic()
        try:
            while True:
                if cancelled():
                    raise Cancelled()
                now = time.monotonic()
                if now - last_data > DECODE_IDLE_TIMEOUT or now - started > max(300., duration * 4):
                    raise TimeoutError('读取视频音轨超时，请检查文件或磁盘后重试')
                try:
                    data = pending.get(timeout=.1)
                except queue.Empty:
                    continue
                if isinstance(data, Exception):
                    raise data
                if not data:
                    break
                last_data = time.monotonic()
                yield data
            while process.poll() is None:
                if cancelled():
                    raise Cancelled()
                if time.monotonic() - last_data > 30:
                    raise TimeoutError('音轨解码进程未正常退出')
                time.sleep(.1)
            if process.returncode:
                errors.seek(0)
                raise RuntimeError(errors.read().decode('utf-8', 'replace')[-1000:])
        finally:
            stop.set()
            _stop_process(process)
            worker.join(timeout=2)
            process.stdout.close()


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
            'source_identity': source_identity(p),
            'width': video.get('width'), 'height': video.get('height'),
            'video_codec': video['codec_name'], 'audio_codec': audio['codec_name']}


def extract_features(source, duration, progress=lambda *_: None, cancelled=lambda: False):
    """Decode mono 8 kHz audio, retaining spectral and speech-activity summaries."""
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source), '-map', '0:a:0',
               '-vn', '-ac', '1', '-ar', '8000', '-f', 'f32le', 'pipe:1']
    energies, spectra, speech_activity = [], [], []
    try:
        import webrtcvad
        vad = webrtcvad.Vad(3)  # conservative mode favors clear conversational speech
    except (ImportError, AttributeError):
        vad = None
    edges = np.unique(np.geomspace(1, 257, 33).astype(int))
    for data in _decoded_chunks(command, 8000 * 4, duration, cancelled):
        samples = np.frombuffer(data, dtype='<f4')
        energies.append(float(20 * np.log10(np.sqrt(np.mean(samples ** 2)) + 1e-9)))
        padded = np.pad(samples, (0, (-len(samples)) % 512))
        power = np.abs(np.fft.rfft(padded.reshape(-1, 512) * np.hanning(512), axis=1)) ** 2
        bands = np.array([power[:, a:b].mean() for a, b in zip(edges[:-1], edges[1:])])
        spectra.append(np.log1p(bands))
        if vad is not None:
            pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2', copy=False)
            frames = [pcm[pos:pos+160].tobytes() for pos in range(0, len(pcm)-159, 160)]
            speech_activity.append(sum(vad.is_speech(frame, 8000) for frame in frames) / max(1, len(frames)))
        if len(energies) % 60 == 0:
            progress(min(.85, .85 * len(energies) / duration), f'已分析 {len(energies)//60} / {math.ceil(duration/60)} 分钟音频')
    if len(energies) < min(3, duration * .8) or len(energies) < duration - max(10, duration * .01):
        raise ValueError('音轨解码不完整，无法为整段视频生成可靠时间表')
    return np.asarray(energies), np.asarray(spectra), (
        np.asarray(speech_activity, dtype=np.float32) if vad is not None else None)


def extract_sound_scores(source, duration, progress=lambda *_: None, cancelled=lambda: False):
    """Run YAMNet over 16 kHz audio in bounded-memory, overlapping batches."""
    from concert_sound_classifier import load_classifier

    classify = load_classifier()
    if classify is None:
        return None
    command = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source), '-map', '0:a:0',
               '-vn', '-ac', '1', '-ar', '16000', '-f', 'f32le', 'pipe:1']
    # A patch spans 95 STFT hops plus the 25 ms STFT window: 0.975 s.
    # Emit only complete windows until EOF; retain the next global hop exactly.
    hop_samples, window_samples = 7680, 15600
    batch_samples = hop_samples * 64
    speech_scores, music_scores, times = [], [], []
    consumed, base = 0, 0
    pending = np.empty(0, dtype=np.float32)
    def collect(values, complete_only):
        speech, music = classify(values)
        count = min(len(speech), len(music))
        if len(speech) != len(music) or not np.all(np.isfinite(speech)) or not np.all(np.isfinite(music)):
            raise ValueError('YAMNet 分类结果不完整，请重新下载模型')
        if complete_only:
            complete = max(0, (len(values) - window_samples) // hop_samples + 1)
            if count < complete:
                raise ValueError('YAMNet 分类窗口不完整，请重新下载模型')
            count = complete
        for index in range(count):
            offset = (base + index * hop_samples) / 16000
            if offset < duration:
                times.append(offset)
                speech_scores.append(float(speech[index]))
                music_scores.append(float(music[index]))
        return count
    for data in _decoded_chunks(command, batch_samples * 4, duration, cancelled):
        samples = np.frombuffer(data[:len(data) - len(data) % 4], dtype='<f4')
        consumed += len(samples)
        pending = np.concatenate((pending, samples))
        count = collect(pending, True) if len(pending) >= window_samples else 0
        pending = pending[count * hop_samples:].copy()
        base += count * hop_samples
        progress(.85 + .15 * min(1.0, consumed / 16000 / max(1.0, duration)),
                 f'正在区分讲话与音乐：{consumed/16000/60:.0f} / {duration/60:.0f} 分钟')
    if len(pending):
        if cancelled():
            raise Cancelled()
        collect(pending, False)
    return {'speech': np.asarray(speech_scores, dtype=np.float32),
            'music': np.asarray(music_scores, dtype=np.float32),
            'times': np.asarray(times, dtype=np.float64), 'hop': hop_samples / 16000,
            'window': window_samples / 16000}


def detect_speech_ranges(sound_scores, duration, activity=None, threshold=.20,
                         music_ratio=.55, min_duration=1.44, max_gap_frames=1):
    """Find speech-like YAMNet windows that stand apart from music for review.

    WebRTC VAD is retained as supporting context only: it frequently responds to
    amplified singing and instruments, so it must not create speech markers by
    itself. These suggestions never change cuts or exports automatically.
    """
    if not sound_scores:
        return []
    speech = np.asarray(sound_scores.get('speech', []), dtype=float).reshape(-1)
    music = np.asarray(sound_scores.get('music', []), dtype=float).reshape(-1)
    hop = float(sound_scores.get('hop', .48))
    count = min(len(speech), len(music))
    if count == 0 or not math.isfinite(hop) or hop <= 0:
        return []
    speech, music = speech[:count], music[:count]
    times = np.asarray(sound_scores.get('times', np.arange(count) * hop), dtype=float).reshape(-1)
    if len(times) < count or not np.all(np.isfinite(times)) or np.any(np.diff(times) <= 0):
        raise ValueError('讲话分类时间轴无效，请重新分析')
    times = times[:count]
    window = float(sound_scores.get('window', .96))
    if (not math.isfinite(window) or window <= 0 or not np.all(np.isfinite(speech)) or
            not np.all(np.isfinite(music))):
        raise ValueError('讲话分类结果无效，请重新分析')
    active = (speech >= threshold) & ((speech >= music * music_ratio) | (speech >= .45))
    vad_by_frame = None
    if activity is not None:
        values = np.asarray(activity, dtype=float).reshape(-1)
        if len(values):
            seconds = np.minimum(len(values)-1, np.maximum(0, (times + window / 2).astype(int)))
            vad_by_frame = values[seconds]
            # Strong YAMNet speech evidence can stand alone; weaker evidence needs VAD support.
            active &= (vad_by_frame >= .25) | (speech >= .50)
    # Bridge a short model flicker inside a spoken phrase, but never span long music.
    i = 0
    while i < count:
        if active[i]:
            i += 1
            continue
        start = i
        while i < count and not active[i]:
            i += 1
        if start > 0 and i < count and i - start <= max_gap_frames:
            active[start:i] = True
    ranges, start = [], None
    for i, enabled in enumerate(np.r_[active, False]):
        if enabled and start is None:
            start = i
        elif not enabled and start is not None:
            end = i
            first_time = times[start]
            last_time = times[end - 1] + window
            span = min(float(duration), float(last_time)) - float(first_time)
            if span >= min_duration:
                speech_score = float(np.mean(speech[start:end]))
                music_score = float(np.mean(music[start:end]))
                ranges.append({'start': max(0.0, float(first_time)),
                               'end': min(float(duration), float(last_time)),
                               'score': round(speech_score, 3),
                               'speech_score': round(speech_score, 3),
                               'music_score': round(music_score, 3),
                               'vad_activity': round(float(np.mean(vad_by_frame[start:end])), 3)
                                               if vad_by_frame is not None else None,
                               'kind': 'speech_vs_music', 'reviewed': False})
            start = None
    return ranges


def suggest(energy, spectra, duration, min_length=120, sensitivity='balanced', speech_activity=None,
            sound_scores=None):
    energy, spectra = np.asarray(energy, dtype=float), np.asarray(spectra, dtype=float)
    if (energy.ndim != 1 or not len(energy) or spectra.ndim != 2 or
            len(spectra) != len(energy) or not spectra.shape[1] or
            not np.all(np.isfinite(energy)) or not np.all(np.isfinite(spectra))):
        raise ValueError('音频特征无效，请重新分析')
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
    raw_change = gaussian_filter1d(np.linalg.norm(before - after, axis=1), 2)
    scale = max(float(np.percentile(raw_change, 95)), .08)
    change = np.clip(raw_change / scale, 0, 1)
    # Sustained structural changes can stand alone, while isolated transient
    # timbre changes below an absolute floor cannot force a cut.
    structural = .75 * np.clip((raw_change - .20) / .70, 0, 1)
    score = np.maximum(.68 * dip + .32 * change, structural)
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
    speech_ranges = detect_speech_ranges(sound_scores, duration, activity=speech_activity)
    points = [0.] + [c['time'] for c in candidates] + [duration]
    segments = [{'title': f'片段 {i+1:02d}', 'start': a, 'end': b, 'selected': True}
                for i, (a, b) in enumerate(zip(points[:-1], points[1:]))]
    hop = max(1, math.ceil(len(energy)/2400))
    waveform = [{'time': i, 'db': round(float(np.max(energy[i:i+hop])), 1)} for i in range(0, len(energy), hop)]
    classifier_available = sound_scores is not None
    notice = ('边界依据音量/音色变化；橙色区间由 YAMNet 讲话/音乐分类提示，WebRTC VAD 活动率作辅助参考，只供试听复核，不自动剪切。分类分数不是演唱会场景校准概率，喊麦、合唱及观众互动仍可能误报。'
              if classifier_available else
              '边界依据音量/音色变化；讲话提示模型未安装，因此当前不显示讲话候选。可运行演唱会环境配置以安装轻量本地模型。')
    return {'candidates': candidates, 'segments': segments, 'waveform': waveform,
            'speech_ranges': speech_ranges,
            'method': 'energy-spectral+yamnet-webrtcvad-v3' if classifier_available else 'energy-spectral-v1',
            'min_length': min_length, 'sensitivity': sensitivity, 'notice': notice}


def validate_segments(segments, duration):
    if not isinstance(segments, list) or not 1 <= len(segments) <= 500:
        raise ValueError('请提供 1～500 个片段')
    clean, previous = [], 0.
    for i, item in enumerate(segments):
        if not isinstance(item, dict) or isinstance(item.get('start'), bool) or isinstance(item.get('end'), bool):
            raise ValueError(f'第 {i+1} 段需提供有效起止时间')
        if not isinstance(item.get('selected', True), bool):
            raise ValueError(f'第 {i+1} 段导出选项需为 true 或 false')
        a, b = float(item['start']), float(item['end'])
        if not all(map(math.isfinite, (a, b))) or a < 0 or b > duration + .05 or b-a < .2:
            raise ValueError(f'第 {i+1} 段时间无效，需在视频范围内且至少 0.2 秒')
        if a < previous - .001:
            raise ValueError(f'第 {i+1} 段与上一段重叠或顺序错误')
        previous = b
        title = str(item.get('title') or f'片段 {i+1:02d}').strip()[:100]
        clean.append({'start': a, 'end': min(b, duration), 'title': title, 'selected': bool(item.get('selected', True))})
    return clean


def export_segments(source, segments, directory, mode, progress=lambda *_: None, cancelled=lambda: False,
                    completed_files=None, on_complete=lambda *_: None, expected_identity=None):
    if mode not in {'copy', 'precise'}:
        raise ValueError('无效导出模式')
    selected = [s for s in segments if s['selected']]
    if not selected:
        raise ValueError('请至少选中一个片段')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=completed_files is not None)
    results = []
    previous = {f['file']: f for f in (completed_files or [])}
    total = sum(s['end'] - s['start'] for s in selected)
    completed = 0
    def save_manifest(state, error=None):
        published = {**previous, **{f['file']: f for f in results}}
        manifest = {'source': str(source), 'source_identity': expected_identity, 'mode': mode,
                    'segments': list(published.values()), 'planned_segments': selected, 'state': state,
                    'notice': '快速模式切点受关键帧影响，实际片段可能包含切点前画面。' if mode == 'copy' else '精确重编码导出。'}
        if error:
            manifest['error'] = str(error)
        temporary = directory / 'manifest.tmp'
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(directory / 'manifest.json')
    save_manifest('exporting')
    try:
        for i, s in enumerate(selected):
            if cancelled():
                raise Cancelled()
            verify_source(source, expected_identity)
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', s['title']).strip(' .')[:80] or '片段'
            target = directory / f'{i+1:02d}_{name}{".mkv" if mode == "copy" else ".mp4"}'
            if target.name in previous:
                old = previous[target.name]
                if (not target.is_file() or target.stat().st_size != old.get('size') or
                        any(old.get(k) != s[k] for k in ('start', 'end', 'title'))):
                    raise ValueError('已完成的导出文件已改变，请新建导出批次')
                if old.get('file_identity'):
                    verify_source(target, old['file_identity'])
                results.append(old)
                completed += s['end'] - s['start']
                on_complete(old)
                save_manifest('exporting')
                continue
            partial = target.with_name(target.stem + '.partial' + target.suffix)
            partial.unlink(missing_ok=True)
            command = ['ffmpeg', '-v', 'error', '-nostdin', '-n', '-ss', str(s['start']), '-i', str(source),
                       '-t', str(s['end']-s['start']), '-map', '0:v:0', '-map', '0:a:0', '-sn', '-dn', '-map_chapters', '-1']
            if mode == 'copy':
                command += ['-c', 'copy', '-avoid_negative_ts', 'make_zero']
            else:
                command += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                            '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2',
                            '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart']
            command += ['-progress', str(directory/'ffmpeg-progress.txt'), '-nostats', str(partial)]
            (directory/'ffmpeg-progress.txt').unlink(missing_ok=True)
            with (directory/'ffmpeg.log').open('ab') as log:
                process = _launch(command, stdout=log, stderr=log)
                started = last_change = time.monotonic()
                last_size, last_fraction = -1, -1
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
                        size = partial.stat().st_size if partial.exists() else 0
                        if fraction != last_fraction or size != last_size:
                            last_change = time.monotonic()
                            last_fraction, last_size = fraction, size
                        now = time.monotonic()
                        if now - last_change > EXPORT_IDLE_TIMEOUT or now - started > max(600., (s['end']-s['start']) * 20):
                            raise TimeoutError('导出长时间没有进展，请检查磁盘后继续本批次')
                        progress((completed+fraction)/total, f'正在导出 {i+1}/{len(selected)}：{s["title"]}')
                        time.sleep(.1)
                    if cancelled():
                        raise Cancelled()
                    if process.returncode:
                        raise RuntimeError('FFmpeg 导出失败：' + (directory/'ffmpeg.log').read_text(encoding='utf-8', errors='replace')[-900:])
                    verify_source(source, expected_identity)
                    partial.rename(target)
                finally:
                    _stop_process(process)
                    partial.unlink(missing_ok=True)
            result = {**s, 'file': target.name, 'size': target.stat().st_size,
                      'file_identity': source_identity(target)}
            results.append(result)
            completed += s['end']-s['start']
            save_manifest('exporting')
            on_complete(result)
        save_manifest('done')
    except BaseException as exc:
        save_manifest('cancelled' if isinstance(exc, Cancelled) else 'error', exc)
        raise
    return results
