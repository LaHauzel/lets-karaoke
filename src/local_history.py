"""Discover saved outputs without depending on the server's in-memory jobs."""
import base64
import json
import copy
import os
import shutil
from pathlib import Path


def identify(directory, root):
    relative = directory.resolve().relative_to(root.resolve()).as_posix()
    return 'local-' + base64.urlsafe_b64encode(relative.encode()).decode().rstrip('=')


def resolve(identifier, root, web_root):
    if not isinstance(identifier, str):
        raise ValueError('无效历史编号')
    if identifier.startswith('local-'):
        encoded = identifier[6:]
        relative = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)).decode()
        directory = (root / relative).resolve()
    else:
        directory = (web_root / identifier).resolve()
    if not identifier or not directory.is_relative_to(root.resolve()) or directory == root.resolve():
        raise ValueError('无效历史目录')
    return directory


def describe(directory, root):
    directory, root = directory.resolve(), root.resolve()
    versions = []
    for path in directory.glob('align*.json'):
        if path.name != 'align.json' and not (path.stem.startswith('align_v') and path.stem[7:].isdigit()):
            continue
        version = 0 if path.name == 'align.json' else int(path.stem[7:])
        suffix = f'_v{version}' if version else ''
        videos = sorted(directory.glob(f'*_karaoke{suffix}.mp4'))
        if not videos and not version and (directory/'karaoke.mp4').exists():
            videos = [directory/'karaoke.mp4']
        details = json.loads(path.read_text(encoding='utf-8'))
        versions.append({'version': version, 'video': videos[0].name if videos else None,
                         'operation': details.get('operation'), 'anchor_row': details.get('anchor_row'),
                         'anchor_rows': details.get('anchor_rows') or ([details['anchor_row']] if details.get('anchor_row') is not None else []),
                         'base_version': details.get('base_version', 0),
                         'align': path.name, 'ass': f'karaoke{suffix}.ass',
                         'srt': f'lyrics{suffix}.srt', 'created': path.stat().st_mtime})
    versions.sort(key=lambda v: v['version'])
    metadata = {}
    if (directory/'job.json').exists():
        try:
            metadata = json.loads((directory/'job.json').read_text(encoding='utf-8'))
        except (ValueError, OSError):
            pass
    latest = next((v for v in reversed(versions) if v['video']), None)
    status = {}
    if (directory/'history_status.json').exists():
        status = json.loads((directory/'history_status.json').read_text(encoding='utf-8'))
    if not metadata and (directory/'task_request.json').exists():
        request = json.loads((directory/'task_request.json').read_text(encoding='utf-8'))
        metadata = request.get('config', {})
    result = None
    if latest:
        details = json.loads((directory/latest['align']).read_text(encoding='utf-8'))
        saved = status.get('result') or {}
        stats = {**saved.get('stats', {}), 'lines': len(details.get('lines', [])),
                 'tokens': sum(len(l.get('tokens', [])) for l in details.get('lines', [])),
                 'health': details.get('health', {}), 'lang': details.get('lang', metadata.get('lang'))}
        result = {**saved, **latest, 'stats': stats}
        for kind in ('asr', 'whisper', 'sofa'):
            result[kind+'_files'] = sorted(p.name for p in directory.glob(kind+'_*') if p.is_file())
        try:
            from alignment_review import attach_review
            result['acceptance'] = attach_review(directory, details)['acceptance']
        except (ValueError, KeyError, OSError, TypeError):
            result['acceptance'] = None
    state = status.get('state', 'incomplete')
    if latest and state not in {'queued', 'running', 'interrupted'}:
        state = 'done'
    return {'job': identify(directory, root), 'title': Path(metadata.get('media') or directory.name).name,
            'path': directory.relative_to(root).as_posix(), 'versions': versions,
            'editable': bool(metadata and (directory/'align.json').exists()),
            'created': max((v['created'] for v in versions), default=directory.stat().st_mtime),
            'state': state, 'error':status.get('error'),
            'resumable': state in {'interrupted','error','cancelled'} and (directory/'task_request.json').exists(),
            'result': result}


_CACHE = {}


def invalidate(directory=None):
    if directory is None:
        _CACHE.clear()
    else:
        _CACHE.pop(str(Path(directory).resolve()), None)


def scan(root):
    directories = set()
    excluded = {'.tasks', '.uploads', 'alignment-repair-audit', 'evaluation', 'concert'}
    for base, folders, names in os.walk(root, followlinks=False):
        folders[:] = [f for f in folders if f not in excluded and not f.startswith('project_audit_')]
        if set(names) & {'job.json', 'align.json', 'history_status.json', 'task_request.json'}:
            directories.add(Path(base))
            folders[:] = []
    records, errors = [], []
    for directory in directories:
        if not directory.resolve().is_relative_to(root.resolve()):
            continue
        # Offline alignment audits may contain job.json/align.json for a dry
        # run, but they are not user history records or playable versions.
        if directory.resolve().is_relative_to((root/'alignment-repair-audit').resolve()):
            continue
        try:
            key = str(directory.resolve())
            signature = tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                                     for p in directory.iterdir() if p.is_file()))
            cached = _CACHE.get(key)
            if not cached or cached[0] != signature:
                cached = (signature, describe(directory, root))
                _CACHE[key] = cached
            records.append(copy.deepcopy(cached[1]))
        except (OSError, ValueError) as exc:
            errors.append(f'{directory.name}: {exc}')
    return {'records': sorted(records, key=lambda r: -r['created']), 'errors': errors}


def delete(root, identifiers, web_root, active=()):
    """Delete only resolved history task directories; never delete the root."""
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError('没有选择历史记录')
    active_paths = set()
    for value in active:
        if isinstance(value, Path):
            active_paths.add(value.resolve())
        else:
            active_paths.add(resolve(value, root, web_root))
    targets = []
    removed = []
    for identifier in dict.fromkeys(identifiers):
        directory = resolve(identifier, root, web_root)
        if not directory.is_dir():
            continue
        if any(path == directory or path.is_relative_to(directory) for path in active_paths):
            raise ValueError(f'任务仍在运行，不能删除：{identifier}')
        markers = ('job.json', 'align.json', 'history_status.json', 'task_request.json')
        if not any((directory/name).is_file() for name in markers):
            raise ValueError('只能删除具体历史任务，不能删除容器目录')
        if any(p.parent != directory for name in markers for p in directory.rglob(name)):
            raise ValueError('不能删除包含其他历史记录的目录')
        targets.append((identifier, directory))
    # Validate the whole selection before deleting any record.
    for identifier, directory in targets:
        if not directory.resolve().is_relative_to(root.resolve()) or directory.resolve() == root.resolve():
            raise ValueError('无效历史目录')
        shutil.rmtree(directory)
        invalidate(directory)
        removed.append(identifier)
    return removed
