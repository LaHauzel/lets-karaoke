"""Build local publish packages from human-approved videos for Bilibili and Douyin.

A package is a self-contained folder: one MP4, one cover, ``publish.json`` with
every field the platform form needs, ``upload-info.txt`` for copy/paste, and a
SHA-256 list. Nothing here logs in, uploads, changes the approved source, or
touches the review API; publishing stays a deliberate human action.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from filelock import FileLock

from repair_alignment import atomic, read

ROOT = Path(__file__).resolve().parents[1]
PUBLISH_ROOT = ROOT / 'out/publish'

CONTENT_WARNING = '发布前请确认音频、画面和歌词均有发布授权；本工具不改变成品、不登录账号、不自动上传。'

PLATFORMS = {
    'bilibili': {
        'name': 'B站',
        'short': 'B站',
        'url': 'https://member.bilibili.com/platform/upload/video/frame',
        'fields': [
            dict(key='title', label='标题', type='text', max=80, required=True,
                 hint='B站稿件标题，最多 80 字'),
            dict(key='desc', label='简介', type='textarea', max=250,
                 hint='最多 250 字；可写曲目、场次和版权说明'),
            dict(key='tags', label='标签', type='text', max=200,
                 hint='多个标签用英文逗号分隔，总长不超过 200 字'),
            dict(key='partition', label='分区 tid', type='text', max=20,
                 hint='可留空，在投稿页手动选择分区'),
            dict(key='copyright', label='版权类型', type='choice', required=True,
                 choices=[['original', '原创'], ['reprint', '转载']]),
            dict(key='reprint_source', label='转载来源', type='text', max=200,
                 hint='版权类型为转载时填写'),
            dict(key='no_reprint', label='禁止转载', type='bool'),
            dict(key='publish_at', label='定时发布', type='text', max=40,
                 hint='例如 2026-11-01 20:00；留空表示立即发布'),
        ],
        'video': {'formats': ['.mp4'], 'max_size': 4 * 1024 ** 3, 'max_duration': 5 * 3600,
                  'max_dimension': 4096, 'max_fps': 120, 'max_audio_bitrate': 320000,
                  'max_audio_channels': 2, 'max_audio_rate': 48000, 'limits_are_advisory': False},
        'cover': {'width': 1146, 'height': 717, 'min_width': 960, 'min_height': 600,
                  'max_bytes': 5 * 1024 ** 2, 'format': 'jpg'},
    },
    'douyin': {
        'name': '抖音',
        'short': '抖音',
        'url': 'https://creator.douyin.com/creator-micro/content/upload',
        'fields': [
            dict(key='title', label='标题', type='text', max=55, required=True,
                 hint='最多 55 字；以抖音投稿页当前限制为准'),
            dict(key='desc', label='文案', type='textarea', max=1000,
                 hint='作品描述；可包含曲目与场次信息'),
            dict(key='topics', label='话题', type='textarea', max=200,
                 hint='每行一个话题，最多 5 个，例如 #现场'),
            dict(key='visibility', label='可见范围', type='choice', required=True,
                 choices=[['public', '公开'], ['friends', '好友可见'], ['private', '仅自己可见']]),
            dict(key='allow_download', label='允许下载', type='bool'),
            dict(key='location', label='位置', type='text', max=100),
            dict(key='collection', label='合集', type='text', max=100),
            dict(key='mentions', label='@好友', type='text', max=100),
            dict(key='publish_at', label='定时发布', type='text', max=40,
                 hint='例如 2026-11-01 20:00；留空表示立即发布'),
            dict(key='original_claim', label='原创声明', type='bool'),
        ],
        'video': {'formats': ['.mp4'], 'max_size': 16 * 1024 ** 3, 'max_duration': 3600,
                  'max_dimension': 4096, 'max_fps': 120, 'max_audio_bitrate': 320000,
                  'max_audio_channels': 2, 'max_audio_rate': 48000,
                  'soft_duration': 15 * 60, 'limits_are_advisory': True},
        'cover': {'width': 1080, 'height': 1440, 'min_width': 1080, 'min_height': 1440,
                  'max_bytes': 5 * 1024 ** 2, 'format': 'jpg'},
    },
}
PLATFORM_ORDER = ['bilibili', 'douyin']
VIDEO_MODE_CHOICES = ['transcode', 'copy']
AUDIO_CHOICES = ['original', 'accompaniment']
SUBTITLE_CHOICES = ['none', 'burn']
SUBTITLE_LANGUAGE_CHOICES = ['original', 'bilingual']
FRAMING_CHOICES = ['source', 'vertical_blur']


def platform_specs():
    """Serializable copy for the workspace UI."""
    return {key: {k: v for k, v in spec.items() if k != 'fields'} |
            {'fields': [dict(field) for field in spec['fields']]}
            for key, spec in PLATFORMS.items()}


def _now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')


def _segment(value, label):
    text = str(value or '')
    cleaned = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff._-]+', '_', text).strip('._')
    if not cleaned or cleaned in {'.', '..'} or len(cleaned) > 100:
        raise ValueError(f'无效的{label}')
    return cleaned


def _track_parts(track_id):
    text = str(track_id or '')
    project, separator, track = text.partition('/')
    if not separator or not track or track.count('/'):
        raise ValueError('曲目标识无效')
    return _segment(project, '项目'), _segment(track, '曲目')


def track_dir(track_id, root=PUBLISH_ROOT):
    project, track = _track_parts(track_id)
    return Path(root) / project / track


def package_dir(track_id, platform, root=PUBLISH_ROOT):
    if platform not in PLATFORMS:
        raise ValueError('未知发布平台')
    return track_dir(track_id, root) / platform


def settings_path(root=PUBLISH_ROOT):
    return Path(root) / 'settings.json'


def _empty_settings():
    return dict(schema_version=1, tracks={})


def _valid_settings_shape(data):
    return (isinstance(data, dict) and data.get('schema_version') == 1
            and isinstance(data.get('tracks'), dict)
            and all(isinstance(key, str) and isinstance(value, dict)
                    for key, value in data['tracks'].items()))


def load_settings(root=PUBLISH_ROOT):
    path = settings_path(root)
    data = read(path, _empty_settings())
    if not _valid_settings_shape(data):
        raise ValueError(f'发布设置格式不正确：{path}')
    return data


def save_settings(track_id, settings, root=PUBLISH_ROOT):
    if not isinstance(settings, dict):
        raise ValueError('发布设置必须是对象')
    _track_parts(track_id)
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        data = load_settings(root)
        data['tracks'][str(track_id)] = settings
        atomic(path, data)
    return settings


def _merge(base, value):
    result = json.loads(json.dumps(base))
    if not isinstance(value, dict):
        return result

    def walk(target, source):
        for key, item in source.items():
            if isinstance(item, dict) and isinstance(target.get(key), dict):
                walk(target[key], item)
            else:
                target[key] = item

    walk(result, value)
    return result


def default_settings(candidate):
    subtitles = candidate.get('subtitles') or []
    original = next((item for item in subtitles if not item.get('bilingual')), None)
    bilingual = next((item for item in subtitles if item.get('bilingual')), None)
    chosen = original or bilingual
    duration = float(candidate.get('duration') or 0)
    cover_time = min(3.0, max(0.0, round(duration * 0.1, 2))) if duration > 4 else 0.5
    title = candidate.get('title') or candidate.get('track') or ''
    concert = candidate.get('concert') or ''
    common = dict(source_file_id=candidate.get('approved', {}).get('file_id'),
                  audio='original', subtitle='burn' if chosen else 'none',
                  subtitle_language='original' if original else 'bilingual',
                  video_mode='transcode', framing='source', cover_time=cover_time)
    return dict(
        common=common,
        platforms={
            'bilibili': dict(title=title, desc=' · '.join(x for x in (concert, title) if x),
                             tags='', partition='', copyright='original', reprint_source='',
                             no_reprint=False, publish_at=''),
            'douyin': dict(title=title, desc=' '.join(x for x in (concert, title) if x),
                           topics='', visibility='public', allow_download=True, location='',
                           collection='', mentions='', publish_at='', original_claim=False),
        },
        note='',
        enabled={'bilibili': bool(title), 'douyin': bool(title)},
    )


def normalize_settings(candidate, saved):
    return _merge(default_settings(candidate), saved or {})


def load_state(track_id, root=PUBLISH_ROOT):
    path = track_dir(track_id, root) / 'state.json'
    data = read(path, dict(schema_version=1, packages={}, history=[]))
    if (not isinstance(data, dict) or data.get('schema_version') != 1
            or not isinstance(data.get('packages'), dict) or not isinstance(data.get('history'), list)):
        raise ValueError(f'发布记录格式不正确：{path}')
    return data


def _update_state(track_id, root, update):
    path = track_dir(track_id, root) / 'state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + '.lock'):
        data = load_state(track_id, root)
        update(data)
        atomic(path, data)
    return data


def record_generation(track_id, platform, result, root=PUBLISH_ROOT):
    package = result['package']

    def update(data):
        data['packages'][platform] = dict(
            status='generated', platform=platform, generated_at=result['generated_at'],
            package=package, files=result['files'], video_sha256=result['video']['sha256'],
            cover_sha256=result['cover']['sha256'], source_signature=result['source']['signature'],
            warnings=list(result.get('warnings') or []), metadata=result['metadata'],
            submitted=None)
        data['history'].append(dict(event='generated', platform=platform, at=result['generated_at'],
                                    package=package, video_sha256=result['video']['sha256'],
                                    warnings=list(result.get('warnings') or [])))

    return _update_state(track_id, root, update)


def mark_submitted(track_id, platform, *, url='', bvid='', note='', root=PUBLISH_ROOT):
    if platform not in PLATFORMS:
        raise ValueError('未知发布平台')
    if not isinstance(url, str) or len(url) > 1000:
        raise ValueError('链接过长')
    if not isinstance(bvid, str) or len(bvid) > 64:
        raise ValueError('稿件编号过长')
    if not isinstance(note, str) or len(note) > 1000:
        raise ValueError('备注过长')
    stamp = _now()

    def update(data):
        record = data['packages'].get(platform)
        if not record:
            raise ValueError('该平台还没有生成发布包')
        record['submitted'] = dict(url=url.strip(), bvid=bvid.strip(), note=note.strip(), at=stamp)
        data['history'].append(dict(event='submitted', platform=platform, at=stamp,
                                    url=url.strip(), bvid=bvid.strip(), note=note.strip()))

    _update_state(track_id, root, update)
    return dict(platform=platform, submitted=True, at=stamp)


def _sig(path):
    stat = Path(path).stat()
    return f'{stat.st_size}:{stat.st_mtime_ns}'


def _verify_approval(candidate):
    approved = candidate.get('approved') or {}
    path = Path(approved.get('path') or '')
    if not path.is_file():
        raise ValueError('审批通过的视频已不存在，请回到成品复核重新确认')
    if _sig(path) != approved.get('signature'):
        raise ValueError('审批后视频已变化，请回到成品复核重新确认')


def probe_media(path):
    path = Path(path).resolve()
    try:
        info = json.loads(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
            capture_output=True, check=True).stdout.decode('utf-8'))
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError(f'无法读取视频信息：{exc}') from exc
    streams = info.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video:
        raise ValueError('视频文件没有画面轨道')
    fps_text = video.get('avg_frame_rate') or video.get('r_frame_rate') or '0/1'
    try:
        numerator, denominator = fps_text.split('/')
        fps = float(numerator) / float(denominator) if float(denominator) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    audio = []
    for stream in streams:
        if stream.get('codec_type') != 'audio':
            continue
        audio.append(dict(index=len(audio), codec=stream.get('codec_name'),
                          channels=int(stream.get('channels') or 0),
                          sample_rate=int(stream.get('sample_rate') or 0),
                          bit_rate=int(stream.get('bit_rate') or 0),
                          title=(stream.get('tags') or {}).get('title', '')))
    duration = info.get('format', {}).get('duration') or video.get('duration') or 0
    return dict(path=str(path), size=path.stat().st_size, duration=float(duration),
                format=info.get('format', {}).get('format_name', ''),
                video=dict(codec=video.get('codec_name'), width=int(video.get('width') or 0),
                           height=int(video.get('height') or 0), fps=round(fps, 3),
                           bit_rate=int(video.get('bit_rate') or 0)),
                audio=audio, subtitles=sum(1 for s in streams if s.get('codec_type') == 'subtitle'),
                streams=len(streams))


def candidates(catalog):
    """Approved, signature-matched tracks that may be published."""
    items, skipped = [], 0
    for track in catalog.get('tracks', []):
        review = track.get('human_review') or {}
        file = next((f for f in track.get('files', []) if f.get('id') == review.get('file_id')), None)
        if (review.get('status') != 'passed' or review.get('stale') or review.get('new_version')
                or not file or not file.get('exists') or file.get('signature') != review.get('file_signature')):
            skipped += 1
            continue
        subtitles = []
        for item in track.get('supporting', []):
            path = item.get('path') or ''
            name = Path(path).name
            if name.lower().endswith('.ass') and item.get('exists', True):
                subtitles.append(dict(name=name, path=str(path), bilingual=name.startswith('bilingual')))
        items.append(dict(
            id=track['id'], project=track['project'], concert=track.get('concert'),
            track=track['track'], title=track.get('title') or track['track'],
            number=track.get('number'), duration=track.get('duration'), grade=track.get('grade'),
            approved=dict(file_id=file['id'], path=str(file['path']), signature=file.get('signature'),
                          kind=file.get('kind'), variant=file.get('variant'),
                          duration=file.get('duration'), bytes=file.get('bytes'),
                          multitrack=file.get('multitrack')),
            subtitles=subtitles,
            review=dict(notes=review.get('notes', ''), updated_at=review.get('updated_at'),
                        file_path=review.get('file_path')),
            revision=(track.get('revision') or {}).get('revision')))
    return items, skipped


def _audio_index(common, probe):
    if common.get('audio') == 'accompaniment':
        if len(probe['audio']) < 2:
            raise ValueError('该文件只有一个音轨，不能选择伴奏')
        return 1
    return 0


def _subtitle_path(candidate, common):
    if common.get('subtitle') != 'burn':
        return None
    wanted_bilingual = common.get('subtitle_language') == 'bilingual'
    subset = [item for item in candidate.get('subtitles') or []
              if bool(item.get('bilingual')) == wanted_bilingual]
    if not subset:
        subset = candidate.get('subtitles') or []
    if not subset:
        raise ValueError('没有可烧录的 ASS 字幕；请选择“不烧录字幕”或先完成字幕输出')
    path = Path(subset[0]['path'])
    if not path.is_file():
        raise ValueError('字幕文件已不存在，请重新输出字幕')
    return path


def _validate_fields(platform, metadata):
    spec = PLATFORMS[platform]
    errors, warnings = [], []
    for field in spec['fields']:
        value = metadata.get(field['key'])
        if field['type'] == 'bool':
            if type(value) is not bool:
                errors.append(f'{field["label"]}必须是勾选状态')
            continue
        if field['type'] == 'choice':
            allowed = [choice[0] for choice in field['choices']]
            if value not in allowed:
                errors.append(f'{field["label"]}无效，请重新选择')
            continue
        text = str(value or '').strip()
        if field.get('required') and not text:
            errors.append(f'{field["label"]}不能为空')
        if len(text) > field['max']:
            errors.append(f'{field["label"]}不能超过 {field["max"]} 字')
        if field['key'] == 'tags' and text:
            tags = [part.strip() for part in re.split(r'[,，]', text) if part.strip()]
            if len(text) > 200:
                errors.append('标签总长度不能超过 200 字')
            if any(len(tag) > 20 for tag in tags):
                warnings.append('单个标签建议不超过 20 字')
        if field['key'] == 'topics' and text:
            topics = [part.strip() for part in text.splitlines() if part.strip()]
            if len(topics) > 5:
                errors.append('抖音话题最多 5 个')
            if any(len(topic) > 20 for topic in topics):
                warnings.append('单个话题建议不超过 20 字')
    if platform == 'bilibili':
        if metadata.get('copyright') == 'reprint' and not str(metadata.get('reprint_source') or '').strip():
            warnings.append('版权类型为转载时，建议填写转载来源')
        if not str(metadata.get('partition') or '').strip():
            warnings.append('未填写分区 tid，请在投稿页手动选择分区')
        if not str(metadata.get('tags') or '').strip():
            warnings.append('未填写标签，投稿页需要至少一个标签')
    if platform == 'douyin':
        if not str(metadata.get('topics') or '').strip():
            warnings.append('未填写话题，抖音可留空')
    return errors, warnings


def _video_checks(platform, probe, framing='source'):
    spec = PLATFORMS[platform]['video']
    errors, warnings = [], []
    advisory = spec.get('limits_are_advisory')
    if probe['size'] > spec['max_size']:
        (warnings if advisory else errors).append('视频超过平台建议的文件大小')
    if probe['duration'] > spec['max_duration']:
        (warnings if advisory else errors).append('视频超过平台建议的时长')
    if spec.get('soft_duration') and probe['duration'] > spec['soft_duration']:
        warnings.append('短视频平台通常以竖屏短片为主，当前时长较长')
    if max(probe['video']['width'], probe['video']['height']) > spec['max_dimension']:
        (warnings if advisory else errors).append('视频分辨率超过平台建议上限')
    if probe['video']['fps'] > spec['max_fps']:
        (warnings if advisory else errors).append('视频帧率超过平台建议上限')
    for stream in probe['audio']:
        if stream['channels'] > spec['max_audio_channels']:
            (warnings if advisory else errors).append('音频声道数超过平台建议上限')
        if stream['sample_rate'] > spec['max_audio_rate']:
            (warnings if advisory else errors).append('音频采样率超过平台建议上限')
        if stream['bit_rate'] and stream['bit_rate'] > spec['max_audio_bitrate']:
            (warnings if advisory else errors).append('音频码率超过平台建议上限')
    if platform == 'douyin' and probe['video']['width'] > probe['video']['height'] \
            and framing != 'vertical_blur':
        warnings.append('当前为横屏视频，抖音以竖屏为主；使用“竖屏模糊背景”或另行裁切效果更好')
    return errors, warnings


def preflight(candidate, settings, platforms, root=PUBLISH_ROOT):
    settings = normalize_settings(candidate, settings)
    common = settings.get('common') or {}
    errors = []
    try:
        _verify_approval(candidate)
        probe = probe_media(candidate['approved']['path'])
        audio_index = _audio_index(common, probe)
    except (ValueError, OSError) as exc:
        return dict(ok=False, errors=[str(exc)], platforms={}, settings=settings)
    subtitle = None
    try:
        subtitle = _subtitle_path(candidate, common)
    except ValueError as exc:
        errors.append(str(exc))
    if common.get('video_mode') not in VIDEO_MODE_CHOICES:
        errors.append('输出方式无效')
    if common.get('subtitle') not in SUBTITLE_CHOICES:
        errors.append('字幕方式无效')
    if common.get('subtitle_language') not in SUBTITLE_LANGUAGE_CHOICES:
        errors.append('字幕语言无效')
    if common.get('audio') not in AUDIO_CHOICES:
        errors.append('音轨选择无效')
    if common.get('framing') not in FRAMING_CHOICES:
        errors.append('画面适配方式无效')
    try:
        cover_time = float(common.get('cover_time', 0))
    except (TypeError, ValueError):
        cover_time = -1
    if not math.isfinite(cover_time) or not 0 <= cover_time < max(probe['duration'], 0.1):
        errors.append('封面时间点必须落在视频时长内')
    if common.get('video_mode') == 'copy':
        if Path(candidate['approved']['path']).suffix.lower() != '.mp4':
            errors.append('复制模式只能用于 MP4 源文件')
        if common.get('subtitle') == 'burn':
            errors.append('复制模式不能烧录字幕')
        if audio_index != 0:
            errors.append('复制模式只能使用源文件的第一个音轨')
        if common.get('framing') != 'source':
            errors.append('复制模式不能改变画面比例')
    plans = {}
    for platform in platforms:
        if platform not in PLATFORMS:
            errors.append(f'未知平台：{platform}')
            continue
        metadata = (settings.get('platforms') or {}).get(platform) or {}
        field_errors, field_warnings = _validate_fields(platform, metadata)
        video_errors, video_warnings = _video_checks(platform, probe, common.get('framing'))
        plans[platform] = dict(
            platform=platform, output=str(package_dir(candidate['id'], platform, root)),
            metadata=metadata, errors=field_errors + video_errors,
            warnings=field_warnings + video_warnings + [CONTENT_WARNING])
    return dict(ok=not errors and all(not plan['errors'] for plan in plans.values()),
                errors=errors, settings=settings,
                source=dict(path=probe['path'], duration=probe['duration'], size=probe['size'],
                            video=probe['video'], audio=probe['audio'], audio_index=audio_index,
                            subtitle=str(subtitle) if subtitle else None,
                            subtitle_name=subtitle.name if subtitle else None,
                            signature=_sig(candidate['approved']['path'])),
                platforms=plans)


def _run_ffmpeg(command, duration, progress, cancel, stage, cwd=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', cwd=cwd)
    tail = []
    try:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                if len(tail) > 200:
                    del tail[:100]
            if cancel is not None and cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise RuntimeError('生成已取消')
            match = re.match(r'out_time_ms=(\d+)', line)
            if match and duration > 0 and progress:
                percent = min(99, int(int(match.group(1)) / 1e6 / duration * 100))
                progress(percent, stage)
    finally:
        if process.stdout:
            process.stdout.close()
    code = process.wait()
    progress and progress(100, stage)
    if code:
        raise ValueError((stage + '失败：' + '\n'.join(tail[-12:])).strip())


def _even_scale():
    return 'scale=trunc(iw/2)*2:trunc(ih/2)*2'


def _video_command(source, video_out, common, plan):
    cwd = Path(source).parent
    filters = []
    ass_name = None
    if plan['source']['subtitle_name']:
        ass_path = Path(plan['source']['subtitle'])
        cwd = ass_path.parent
        ass_name = ass_path.name
        if ' ' in ass_name or "'" in ass_name:
            ass_name = "'" + ass_name.replace("'", r"\\'") + "'"
    framing = common.get('framing') if framing_ok(common) else 'source'
    if framing == 'vertical_blur':
        chain = ('[0:v]scale=1080:1920:force_original_aspect_ratio=increase,'
                 'crop=1080:1920,boxblur=24:2[bg];'
                 '[0:v]scale=1080:1920:force_original_aspect_ratio=decrease,'
                 'setsar=1[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2')
        chain += f',ass={ass_name}' if ass_name else ''
        chain += f',{_even_scale()}[v]'
        args = ['-filter_complex', chain, '-map', '[v]']
    else:
        vf = _even_scale()
        if ass_name:
            vf = f'ass={ass_name},' + vf
        args = ['-vf', vf, '-map', '0:v:0']
    audio = plan['source']['audio_index']
    args += ['-map', f'0:a:{audio}']
    return args, cwd


def framing_ok(common):
    return common.get('framing') in FRAMING_CHOICES


def _cover_command(source, cover_out, width, height, stamp):
    return ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
            '-ss', f'{max(0.0, float(stamp)):.3f}', '-i', str(source), '-frames:v', '1',
            '-vf', f'scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}',
            '-pix_fmt', 'yuvj444p', '-q:v', '2', str(cover_out)]


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _upload_info(platform, metadata, package):
    spec = PLATFORMS[platform]
    lines = [f'平台：{spec["name"]}', f'投稿入口：{spec["url"]}', '']
    if platform == 'bilibili':
        lines += [f'标题：{metadata.get("title", "")}', f'简介：{metadata.get("desc", "")}',
                  f'标签：{metadata.get("tags", "")}', f'分区 tid：{metadata.get("partition", "")}',
                  f'版权类型：{"原创" if metadata.get("copyright") == "original" else "转载"}',
                  f'转载来源：{metadata.get("reprint_source", "")}',
                  f'禁止转载：{"是" if metadata.get("no_reprint") else "否"}',
                  f'定时发布：{metadata.get("publish_at", "") or "立即"}']
    else:
        visibility_field = next((field for field in spec['fields'] if field['key'] == 'visibility'), None)
        visibility_label = (dict((choice[0], choice[1]) for choice in visibility_field['choices'])
                            .get(metadata.get('visibility'), '') if visibility_field else '')
        lines += [f'标题：{metadata.get("title", "")}', f'文案：{metadata.get("desc", "")}',
                  f'话题：{metadata.get("topics", "")}',
                  f'可见范围：{visibility_label}',
                  f'允许下载：{"是" if metadata.get("allow_download") else "否"}',
                  f'位置：{metadata.get("location", "")}', f'合集：{metadata.get("collection", "")}',
                  f'@好友：{metadata.get("mentions", "")}',
                  f'定时发布：{metadata.get("publish_at", "") or "立即"}',
                  f'原创声明：{"是" if metadata.get("original_claim") else "否"}']
    lines += ['', f'视频：{package}/video.mp4', f'封面：{package}/cover.jpg',
              '', CONTENT_WARNING]
    return '\n'.join(lines) + '\n'


def build_package(candidate, plan, platform, root=PUBLISH_ROOT, progress=None, cancel=None):
    platform_plan = plan['platforms'][platform]
    destination = package_dir(candidate['id'], platform, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f'.build-{platform}-{uuid.uuid4().hex}'
    temporary.mkdir()
    common = plan['settings']['common']
    source = plan['source']
    try:
        progress and progress(0, f'{PLATFORMS[platform]["name"]} · 准备')
        video = (temporary / 'video.mp4').resolve()
        if common.get('video_mode') == 'copy':
            command = ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
                       '-progress', 'pipe:1', '-nostats', '-i', source['path'],
                       '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy',
                       '-movflags', '+faststart', str(video)]
            _run_ffmpeg(command, source['duration'], progress,
                        cancel, f'{PLATFORMS[platform]["name"]} · 复制视频')
            cwd = None
        else:
            args, cwd = _video_command(source['path'], video, common, plan)
            command = ['ffmpeg', '-hide_banner', '-nostdin', '-y', '-loglevel', 'error',
                       '-progress', 'pipe:1', '-nostats', '-i', source['path'],
                       *args, '-c:v', 'libx264', '-preset', 'medium', '-crf', '20',
                       '-pix_fmt', 'yuv420p', '-profile:v', 'high',
                       '-c:a', 'aac', '-b:a', '192k', '-ac', '2', '-ar', '48000',
                       '-map_metadata', '-1', '-map_chapters', '-1',
                       '-movflags', '+faststart', str(video)]
            _run_ffmpeg(command, source['duration'], progress,
                        cancel, f'{PLATFORMS[platform]["name"]} · 转码视频', cwd=cwd)
        progress and progress(0, f'{PLATFORMS[platform]["name"]} · 生成封面')
        cover = (temporary / 'cover.jpg').resolve()
        cover_spec = PLATFORMS[platform]['cover']
        _run_ffmpeg(_cover_command(source['path'], cover, cover_spec['width'],
                                   cover_spec['height'], common.get('cover_time', 0)),
                    None, progress, cancel, f'{PLATFORMS[platform]["name"]} · 封面')
        video_hash = _hash_file(video)
        cover_hash = _hash_file(cover)
        generated = _now()
        package_json = dict(
            schema_version=1, platform=platform, platform_name=PLATFORMS[platform]['name'],
            track=dict(id=candidate['id'], project=candidate['project'], concert=candidate.get('concert'),
                       title=candidate.get('title'), number=candidate.get('number'),
                       revision=candidate.get('revision')),
            source=dict(path=source['path'], signature=source['signature'],
                        file_id=candidate['approved']['file_id'], kind=candidate['approved'].get('kind'),
                        variant=candidate['approved'].get('variant'), duration=source['duration']),
            package=dict(folder=str(destination), video='video.mp4', cover='cover.jpg',
                         info='upload-info.txt'),
            video=dict(mode=common.get('video_mode'), audio=common.get('audio'),
                       subtitle=common.get('subtitle'), subtitle_language=common.get('subtitle_language'),
                       framing=common.get('framing'), file='video.mp4', sha256=video_hash,
                       bytes=video.stat().st_size),
            cover=dict(file='cover.jpg', sha256=cover_hash, width=cover_spec['width'],
                       height=cover_spec['height'], bytes=cover.stat().st_size),
            metadata=platform_plan['metadata'], checks=dict(
                source_duration=source['duration'], source_size=source['size'],
                video=source['video'], audio=source['audio']),
            warnings=list(platform_plan['warnings']), generated_at=generated,
            manual_upload=dict(url=PLATFORMS[platform]['url'],
                               fields=[field['key'] for field in PLATFORMS[platform]['fields']],
                               note=CONTENT_WARNING),
            human_approved=True, automatically_uploaded=False)
        (temporary / 'publish.json').write_text(
            json.dumps(package_json, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')
        (temporary / 'upload-info.txt').write_text(
            _upload_info(platform, platform_plan['metadata'], destination), encoding='utf8')
        (temporary / 'sha256.txt').write_text(
            f'{video_hash}  video.mp4\n{cover_hash}  cover.jpg\n'
            f'{_hash_file(temporary / "publish.json")}  publish.json\n', encoding='utf8')
        if destination.exists():
            import shutil
            shutil.rmtree(destination)
        temporary.rename(destination)
    except Exception:
        import shutil
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    files = {name: str(destination / name) for name in
             ('video.mp4', 'cover.jpg', 'publish.json', 'upload-info.txt', 'sha256.txt')}
    return dict(platform=platform, package=str(destination), files=files,
                metadata=platform_plan['metadata'], warnings=platform_plan['warnings'],
                generated_at=generated, source=dict(path=source['path'], signature=source['signature'],
                                                    duration=source['duration']),
                video=dict(file=files['video.mp4'], sha256=video_hash,
                           bytes=(destination / 'video.mp4').stat().st_size),
                cover=dict(file=files['cover.jpg'], sha256=cover_hash,
                           bytes=(destination / 'cover.jpg').stat().st_size))


def generate(candidate, settings, platforms, root=PUBLISH_ROOT, progress=None, cancel=None):
    plan = preflight(candidate, settings, platforms, root)
    if plan['errors']:
        raise ValueError('; '.join(plan['errors']))
    results = []
    for index, platform in enumerate(platforms):
        platform_plan = plan['platforms'][platform]
        if platform_plan['errors']:
            raise ValueError(f'{PLATFORMS[platform]["name"]}：' + '; '.join(platform_plan['errors']))
        if cancel is not None and cancel.is_set():
            raise RuntimeError('生成已取消')

        def report(percent, stage, base=index, total=len(platforms)):
            if progress:
                progress(int((base + percent / 100) / total * 100), stage)

        result = build_package(candidate, plan, platform, root, report, cancel)
        record_generation(candidate['id'], platform, result, root)
        results.append(result)
    progress and progress(100, '完成')
    return results
