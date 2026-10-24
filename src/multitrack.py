"""Lossless multi-track MKV delivery with explicit variant selections.

The four karaoke combinations share one video, two audio and two ASS streams.
Stream hashes cover every encoded packet; approvals never transfer implicitly.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from filelock import FileLock

SELECTIONS = {1: (0, 0), 2: (1, 0), 3: (0, 1), 4: (1, 1)}


def signature(path):
    stat = Path(path).stat()
    return f'{stat.st_size}:{stat.st_mtime_ns}'


def command(args):
    result = subprocess.run(list(map(str, args)), capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode('utf8', errors='replace')[-4000:])
    return result.stdout


def probe(path):
    return json.loads(command(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-show_data_hash', 'sha256', '-of', 'json', path]))


def codec_headers(info):
    result = {'v': [], 'a': [], 's': []}
    kinds = {'video': 'v', 'audio': 'a', 'subtitle': 's'}
    for stream in info['streams']:
        if stream['codec_type'] in kinds:
            result[kinds[stream['codec_type']]].append({k: stream.get(k) for k in
                ('codec_name','extradata_hash','width','height','sample_rate','channels')})
    return result


def hashes(path):
    """Read the complete file without decoding/re-encoding compressed streams."""
    output = command(['ffmpeg', '-v', 'error', '-nostdin', '-i', path,
                      '-map', '0:v:0', '-map', '0:a', '-map', '0:s',
                      '-c', 'copy', '-f', 'streamhash', '-hash', 'sha256', '-'])
    result = {'v': [], 'a': [], 's': []}
    for line in output.decode('ascii').splitlines():
        fields = line.strip().split(',')
        if len(fields) == 3 and fields[1] in result:
            result[fields[1]].append(fields[2])
    return result


def timelines(path, include_packets=False):
    """Hash packet presentation times and durations at Matroska ms precision."""
    info = json.loads(command(['ffprobe', '-v', 'error', '-show_packets', '-show_streams',
                              '-show_entries', 'packet=stream_index,pts_time,duration_time:stream=index,codec_type',
                              '-of', 'json', path]))
    kinds = {'video': 'v', 'audio': 'a', 'subtitle': 's'}
    streams = {s['index']: [] for s in info['streams'] if s['codec_type'] in kinds}
    for packet in info['packets']:
        if packet['stream_index'] in streams:
            streams[packet['stream_index']].append([
                round(float(packet[k]) * 1000) if k in packet else None for k in ('pts_time', 'duration_time')])
    result = {'v': [], 'a': [], 's': []}
    for stream in info['streams']:
        if stream['codec_type'] in kinds:
            packets = streams[stream['index']]
            first = next((p[0] for p in packets if p[0] is not None), 0)
            values = [[p[0]-first if p[0] is not None else None,p[1]] for p in packets]
            result[kinds[stream['codec_type']]].append(dict(
                packets=len(packets), start_ms=first,
                digest=hashlib.sha256(json.dumps(values).encode()).hexdigest(),
                **({'values': values} if include_packets else {})))
    return result


def shift_timeline(streams, milliseconds):
    return [{**s, 'start_ms': s['start_ms'] - milliseconds} for s in streams]


def compare_timelines(actual, expected):
    """Allow only 1 ms of container time-base rounding, never frame-sized drift."""
    maximum = 0
    for kind in ('v', 'a', 's'):
        if len(actual[kind]) != len(expected[kind]):
            raise ValueError('数据包时间轴的轨道数量不一致')
        for left, right in zip(actual[kind], expected[kind]):
            if left['packets'] != right['packets']:
                raise ValueError('数据包数量发生变化')
            for packet_index, (a, b) in enumerate(zip(left['values'], right['values'])):
                for index in (0, 1):
                    if a[index] is None or b[index] is None:
                        if a[index] != b[index]:
                            # A leading AAC priming packet can lose its duration
                            # (or negative PTS) when stored before time zero.
                            # Its bytes remain hash-verified; all later packets
                            # still require matching presentation timestamps.
                            expected_pts = b[0] + right['start_ms'] if b[0] is not None else None
                            if (kind == 'a' and packet_index == 0 and a[index] is None and
                                    expected_pts is not None and expected_pts <= 0):
                                continue
                            raise ValueError('数据包时间信息缺失')
                        continue
                    delta = abs(a[index] - b[index] + (left['start_ms'] - right['start_ms'] if index == 0 else 0))
                    maximum = max(maximum, delta)
                    if delta > 1:
                        raise ValueError(f'数据包时间轴改变 {delta} 毫秒，请保留原文件')
    return maximum


def selection(variant):
    audio, subtitle = SELECTIONS[variant]
    return dict(audio=audio, subtitle=subtitle, audio_tracks=2, subtitle_tracks=2)


def records(path, duration):
    path = Path(path).resolve()
    return [dict(file=str(path), variant=v, duration=duration, size=path.stat().st_size,
                 verified=True, multitrack=selection(v)) for v in SELECTIONS]


def _metadata():
    return ['-map_metadata', '-1', '-map_chapters', '-1',
            '-metadata:s:a:0', 'title=Original vocals', '-metadata:s:a:1', 'title=Accompaniment',
            '-metadata:s:s:0', 'title=Original lyrics', '-metadata:s:s:1', 'title=Bilingual lyrics',
            '-metadata:s:s:0', 'language=und', '-metadata:s:s:1', 'language=zho',
            '-disposition:a:0', 'default', '-disposition:a:1', '0',
            '-disposition:s:0', 'default', '-disposition:s:1', '0']


def validate(path, duration, expected=None, decode=True, expected_timing=None, expected_headers=None):
    info = probe(path)
    headers = codec_headers(info)
    if expected_headers is not None and headers != expected_headers:
        raise ValueError('多轨文件的编码参数或字幕样式改变')
    streams = info['streams']
    if sorted(s['codec_type'] for s in streams) != ['audio', 'audio', 'subtitle', 'subtitle', 'video']:
        raise ValueError('多轨 MKV 必须包含一路视频、两路音频和两路字幕')
    if any(s['codec_name'] != 'ass' for s in streams if s['codec_type'] == 'subtitle'):
        raise ValueError('字幕格式必须为 ASS')
    if abs(float(info['format']['duration']) - duration) > .6:
        raise ValueError('多轨文件时长与原版本不一致')
    for kind in ('audio', 'subtitle'):
        defaults = [s.get('disposition', {}).get('default', 0) for s in streams if s['codec_type'] == kind]
        if defaults != [1, 0]:
            raise ValueError('多轨默认选择必须为原唱、原文')
    actual = hashes(path)
    if expected is not None and actual != expected:
        raise ValueError('多轨封装改变了音视频或字幕的数据，请保留原文件')
    timing = timelines(path, include_packets=True)
    timing_error = compare_timelines(timing, expected_timing) if expected_timing is not None else None
    timing = {k: [{key: value for key, value in stream.items() if key != 'values'} for stream in group] for k, group in timing.items()}
    if decode:
        command(['ffmpeg', '-v', 'error', '-xerror', '-nostdin', '-ss', max(0, duration / 2 - .5),
                 '-i', path, '-t', '1', '-map', '0:v:0', '-map', '0:a:0', '-map', '0:a:1', '-sn', '-f', 'null', '-'])
    return dict(duration=float(info['format']['duration']), hashes=actual, timelines=timing, signature=signature(path),
                bytes=Path(path).stat().st_size, decoded_sample=decode, timing_max_error_ms=timing_error,
                codec_headers=headers)


def pack_variants(variants, destination):
    """Consolidate four existing soft variants, verifying all source packets."""
    paths = {v: Path(variants[v]).resolve() for v in SELECTIONS}
    destination = Path(destination).resolve()
    if destination in paths.values() or destination.suffix.lower() != '.mkv':
        raise ValueError('多轨文件须使用独立 MKV 路径')
    before = {v: signature(p) for v, p in paths.items()}
    info = {v: probe(p) for v, p in paths.items()}
    source_headers = {v: codec_headers(i) for v, i in info.items()}
    for variant, (audio, subtitle) in SELECTIONS.items():
        if source_headers[variant] != dict(v=source_headers[1]['v'], a=source_headers[1 if audio == 0 else 2]['a'],
                                           s=source_headers[1 if subtitle == 0 else 3]['s']):
            raise ValueError('四种组合的编码参数或字幕样式不一致')
    duration = float(info[1]['format']['duration'])
    if any(abs(float(i['format']['duration']) - duration) > .6 for i in info.values()):
        raise ValueError('四个版本的时长不一致')
    source_hashes = {v: hashes(p) for v, p in paths.items()}
    source_timelines = {v: timelines(p, include_packets=True) for v, p in paths.items()}
    for value in source_hashes.values():
        if [len(value[t]) for t in ('v', 'a', 's')] != [1, 1, 1]:
            raise ValueError('待归档版本须各含一路视频、音频及字幕')
    first = source_hashes[1]
    if any(h['v'] != first['v'] for h in source_hashes.values()):
        raise ValueError('四个版本画面不同，不能合并为同一视频流')
    if source_hashes[1]['a'] != source_hashes[3]['a'] or source_hashes[2]['a'] != source_hashes[4]['a']:
        raise ValueError('原唱或伴奏在不同版本中不一致')
    if source_hashes[1]['s'] != source_hashes[2]['s'] or source_hashes[3]['s'] != source_hashes[4]['s']:
        raise ValueError('不同音轨版本使用了不同字幕')
    offsets = {v: t['v'][0]['start_ms'] - source_timelines[1]['v'][0]['start_ms'] for v, t in source_timelines.items()}
    adjusted = {v: {k: shift_timeline(t[k], offsets[v]) for k in t} for v, t in source_timelines.items()}
    for variant, (audio, subtitle) in SELECTIONS.items():
        timing = adjusted[variant]
        compare_timelines(timing, dict(v=adjusted[1]['v'], a=adjusted[1 if audio == 0 else 2]['a'],
                                        s=adjusted[1 if subtitle == 0 else 3]['s']))
    expected = dict(v=first['v'], a=first['a'] + source_hashes[2]['a'], s=first['s'] + source_hashes[3]['s'])
    expected_timing = dict(v=adjusted[1]['v'], a=adjusted[1]['a'] + adjusted[2]['a'],
                           s=adjusted[1]['s'] + adjusted[3]['s'])
    expected_headers = dict(v=source_headers[1]['v'], a=source_headers[1]['a'] + source_headers[2]['a'],
                            s=source_headers[1]['s'] + source_headers[3]['s'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.stem + '.pending.mkv')
    with FileLock(str(destination) + '.lock'):
        if destination.exists():
            result = validate(destination, duration, expected, expected_timing=expected_timing, expected_headers=expected_headers)
        else:
            try:
                # Some old MKVs have sparse video DTS. Disable FFmpeg's DTS
                # synthesis for the picture stream so it cannot move B-frame
                # PTS. Read audio separately with normal duration handling;
                # nofillin on AAC would accumulate timestamp rounding drift.
                command(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-copyts', '-fflags', '+nofillin+igndts', '-i', paths[1],
                         '-i', paths[1], '-itsoffset', -offsets[2]/1000, '-i', paths[2],
                         '-itsoffset', -offsets[3]/1000, '-i', paths[3],
                         '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0', '-map', '0:s:0', '-map', '3:s:0',
                         '-c', 'copy', '-avoid_negative_ts', 'disabled', *_metadata(), pending])
                result = validate(pending, duration, expected, expected_timing=expected_timing, expected_headers=expected_headers)
                if any(signature(paths[v]) != before[v] for v in paths):
                    raise ValueError('合并期间源文件发生变化')
                pending.replace(destination)
                result['signature'] = signature(destination)
            finally:
                pending.unlink(missing_ok=True)
    return {**result, 'file': str(destination), 'source_signatures': before, 'source_offsets_ms': offsets}


def export_archive(source, accompaniment, normal, bilingual, destination):
    """Create new multi-track output directly, without four temporary videos."""
    source, accompaniment, normal, bilingual = map(Path, (source, accompaniment, normal, bilingual))
    destination = Path(destination).resolve()
    if destination in [p.resolve() for p in (source, accompaniment, normal, bilingual)]:
        raise ValueError('多轨文件不能覆盖输入素材')
    duration = float(probe(source)['format']['duration'])
    if abs(float(probe(accompaniment)['format']['duration']) - duration) > .6:
        raise ValueError('伴奏时长不匹配')
    identity = hashlib.sha256(json.dumps([(str(p.resolve()), signature(p)) for p in
                                         (source, accompaniment, normal, bilingual)]).encode()).hexdigest()
    checkpoint = destination.with_suffix('.mkv.json')
    from repair_alignment import read, atomic
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination) + '.lock'):
        saved = read(checkpoint, {})
        if destination.exists() and saved.get('fingerprint') == identity and saved.get('signature') == signature(destination):
            return records(destination, duration)
        pending = destination.with_name(destination.stem + '.pending.mkv')
        try:
            command(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', source, '-i', accompaniment,
                     '-i', normal, '-i', bilingual, '-map', '0:v:0', '-map', '0:a:0', '-map', '1:a:0',
                     '-map', '2:s:0', '-map', '3:s:0', '-c', 'copy', '-c:s', 'ass', *_metadata(), pending])
            checked = validate(pending, duration)
            pending.replace(destination)
            atomic(checkpoint, {**checked, 'fingerprint': identity, 'signature': signature(destination)})
        finally:
            pending.unlink(missing_ok=True)
    return records(destination, duration)
