"""Discover saved outputs without depending on the server's in-memory jobs."""
import base64
import json
import shutil
from pathlib import Path


def identify(directory, root):
    relative = directory.resolve().relative_to(root.resolve()).as_posix()
    return 'local-' + base64.urlsafe_b64encode(relative.encode()).decode().rstrip('=')


def resolve(identifier, root, web_root):
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
    return {'job': identify(directory, root), 'title': Path(metadata.get('media') or directory.name).name,
            'path': directory.relative_to(root).as_posix(), 'versions': versions,
            'editable': bool(metadata and (directory/'align.json').exists()),
            'created': max((v['created'] for v in versions), default=directory.stat().st_mtime),
            'state': 'done' if latest else status.get('state', 'incomplete'), 'error':status.get('error'),
            'result': {**latest, 'stats': (status.get('result') or {}).get('stats',{})} if latest else None}


def scan(root):
    directories = {p.parent for p in root.rglob('job.json')}
    directories.update(p.parent for p in root.rglob('align.json'))
    directories.update(p.parent for p in root.rglob('history_status.json'))
    web = root/'webui'
    if web.exists():
        directories.update(p for p in web.iterdir() if p.is_dir() and (p/'in').exists())
    records, errors = [], []
    for directory in directories:
        if not directory.resolve().is_relative_to(root.resolve()):
            continue
        try:
            records.append(describe(directory, root))
        except (OSError, ValueError) as exc:
            errors.append(f'{directory.name}: {exc}')
    return {'records': sorted(records, key=lambda r: -r['created']), 'errors': errors}


def delete(root, identifiers, web_root, active=()):
    """Delete only resolved history task directories; never delete the root."""
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError('没有选择历史记录')
    active = set(active)
    removed = []
    for identifier in dict.fromkeys(identifiers):
        if identifier in active:
            raise ValueError(f'任务仍在运行，不能删除：{identifier}')
        directory = resolve(identifier, root, web_root)
        if not directory.is_dir():
            continue
        shutil.rmtree(directory)
        removed.append(identifier)
    return removed
