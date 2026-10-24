"""Read existing delivery manifests; never render or modify media."""
import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1] / 'out/concert-collection/exports'
VARIANTS = {1: '原文 · 原唱', 2: '原文 · 伴奏', 3: '双语 · 原唱', 4: '双语 · 伴奏'}
EXPERIMENTS = ['baseline', 'medium-mix', 'medium-vocals', 'large-v3-mix', 'large-v3-vocals']


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else default


def read_revision_history(path):
    """Accept the versioned envelope and the early bare-list format."""
    data = read_json(path, [])
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and data.get('schema_version') == 1 and isinstance(data.get('revisions'), list):
        return data['revisions']
    raise ValueError('历史修订清单格式不受支持')


def grade(row):
    q = row['quality']
    total = max(1, q['total_rows'])
    fraction = len(q['lower_confidence_rows']) / total
    if row['acceptance']['status'] == 'needs_fix':
        return 'fix'
    if q['alignment_median_model_score'] >= .8 and fraction <= .18 and len(q['higher_confidence_rows']) / total >= .65:
        return 'high'
    return 'low' if q['alignment_median_model_score'] < .6 or fraction > .45 else 'medium'


def revision_lyrics(revision, root):
    """Read lyrics from this release only; never borrow text from older rows."""
    lines = []
    for raw in revision.get('supporting', []):
        path = Path(raw)
        path = (path if path.is_absolute() else root / path).resolve()
        if path.name == 'align.json' and path.is_relative_to(root) and path.is_file():
            lines = read_json(path, {}).get('lines', [])
            break
    for entry in revision.get('evidence', []):
        entry['text'] = entry.get('text') or entry.get('lyrics') or ''
        index = entry['row'] - 1
        if entry['text'] or not 0 <= index < len(lines):
            continue
        line = lines[index]
        if all(abs(entry[key] - line[key]) <= .03 for key in ('start', 'end')):
            entry['text'] = line.get('raw', '')


def build_catalog(root=ROOT, url_prefix=''):
    root = root.resolve()
    allowed = {}
    diagnostics = []
    # Storage migration is an overlay: old manifests and approval identities
    # stay intact, while unreferenced variants resolve to the shared archive.
    from multitrack import signature, selection
    archive_map = {}
    try:
        overlay = read_json(root / 'multitrack-manifest.json', {})
    except (OSError, ValueError) as error:
        diagnostics.append('多轨清单无法读取：' + str(error))
        overlay = {}
    if not isinstance(overlay, dict) or overlay.get('schema_version', 1) != 1:
        diagnostics.append('多轨清单版本不受支持')
        overlay = {}
    for archive in overlay.get('archives', []):
        if not isinstance(archive, dict) or 'file' not in archive or 'signature' not in archive \
                or not isinstance(archive.get('originals'), list):
            diagnostics.append('多轨清单条目缺少必要字段')
            continue
        destination = (root / archive['file']).resolve()
        if not destination.is_relative_to(root):
            raise ValueError('多轨归档路径超出成品库')
        if not destination.is_file() or signature(destination) != archive['signature']:
            diagnostics.append('多轨归档缺失或已改变：' + archive['file'])
            continue
        for original in archive['originals']:
            if not isinstance(original, dict) or 'path' not in original or 'signature' not in original:
                diagnostics.append('多轨清单原片条目缺少必要字段')
                continue
            source = (root / original['path']).resolve()
            if not source.is_relative_to(root):
                raise ValueError('多轨原片路径超出成品库')
            if source.is_file() and signature(source) != original['signature']:
                continue  # A later export superseded this storage migration.
            archive_map[source] = (destination, original)
    retained = []
    metadata = read_json(root / 'library-metadata.json', {})
    project_labels = dict(metadata.get('projects', {}))
    report = (root / (metadata.get('revision_report') or 'revisions/index.html')).resolve()
    if not report.is_relative_to(root):
        diagnostics.append('修订报告路径超出成品库')
        report = root / 'revisions/index.html'

    def asset(raw, kind, variant=None, _archive=True, **extra):
        p = Path(raw)
        p = (p if p.is_absolute() else root / p).resolve()
        if not p.is_relative_to(root):
            raise ValueError('Manifest asset is outside exports')
        if _archive and p in archive_map:
            destination, original = archive_map[p]
            if original.get('keep'):
                retained.append(asset(p, kind, variant, _archive=False,
                                      **{**extra, 'previous_release': True, 'retained_for_review': True}))
            p = destination
            extra['multitrack'] = selection(variant)
        relative = p.relative_to(root).as_posix()
        identity = relative + (f'|selection:{variant}' if extra.get('multitrack') else '')
        asset_id = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
        exists = p.is_file()
        stat = p.stat() if exists else None
        result = dict(id=asset_id, path=str(p), url=url_prefix+'/' + quote(relative, safe='/'),
                      name=p.name, kind=kind, variant=variant, exists=exists,
                      bytes=stat.st_size if exists else 0,
                      signature=f'{stat.st_size}:{stat.st_mtime_ns}' if exists else None, **extra)
        if exists:
            allowed[asset_id] = p
        else:
            diagnostics.append('文件缺失：' + relative)
        return result

    def video_asset(value, kind, **extra):
        return asset(value['file'], kind, value['variant'], duration=value['duration'],
                     **({**extra, 'multitrack': value['multitrack']} if value.get('multitrack') else extra))

    evidence = defaultdict(list)
    csv_path = root / 'confidence-details.csv'
    if csv_path.is_file():
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            for r in csv.DictReader(stream):
                def number(key):
                    return float(r[key]) if r[key] else None
                evidence[(r['project'], r['track'])].append(dict(
                    row=int(r['row']), start=number('clip_start'), end=number('clip_end'),
                    score=number('model_score'), disagreement=number('dual_disagreement_seconds'),
                    priority=r['priority'], reasons=r['reasons'], text=r.get('lyrics', '')))

    hard = {(r['project'], r['track']): r for r in read_json(root / 'hardsub-samples/manifest.json', [])}
    reviewed = {(r['project'], r['track']): r for r in read_json(root / 'reviewed-samples/manifest.json', [])}
    ab = {(r['project'], r['track']): r for r in read_json(root / 'model-comparison/manifest.json', {}).get('cases', [])}
    revisions = {(r['project'], r['track']): r for r in read_json(root / 'revisions/manifest.json', [])}
    revision_history = defaultdict(dict)
    try:
        history = read_revision_history(root / 'revisions/history.json')
    except (OSError, ValueError):
        diagnostics.append('历史修订清单无法读取')
        history = []
    for historical in history:
        if not isinstance(historical, dict) or not {'project', 'track', 'revision'} <= set(historical):
            diagnostics.append('历史修订条目缺少必要字段')
            continue
        revision_history[(historical['project'], historical['track'])][historical['revision']] = historical
    repairs = {(r['project'], r['track']): r for r in read_json(root / 'repair-runs/manifest.json', [])}
    audit = read_json(root / 'revisions/audit.json', {})
    tracks = []
    for row in read_json(root / 'track-manifest.json', []):
        key = row['project'], row['track']
        project_labels.setdefault(row['project'], row.get('concert', row['project']))
        q = row['quality']
        baseline_quality = dict(score=q['alignment_median_model_score'],total=q['total_rows'],
                                high_rows=q['higher_confidence_rows'],low_rows=q['lower_confidence_rows'],
                                grade=grade(row))
        files = []
        retained.clear()
        review = reviewed.get(key)
        for v in (review or {}).get('files', []):
            files.append(video_asset(v, 'reviewed'))
        for v in hard.get(key, {}).get('variants', []):
            files.append(video_asset(v, 'hard',
                               superseded=bool(review and v['variant'] in (3, 4))))
        for v in row['variants']:
            files.append(video_asset(v, 'soft',
                               older_translation=bool(review and v['variant'] in (3, 4))))
        revision = revisions.get(key)
        historical = [r for name,r in revision_history[key].items()
                      if name != (revision or {}).get('revision')]
        for old in historical:
            revision_lyrics(old, root)
            for v in old.get('files', []):
                item = video_asset(v, 'updated' if str(v['file']).endswith('.mp4') else 'soft',
                             revision=old['revision'],
                             timeline_offset=old.get('timeline_offset', 0), previous_release=True)
                if item['id'] not in {f['id'] for f in files}:
                    files.append(item)
        acceptance = (revision or {}).get('acceptance', row['acceptance'])
        health = (revision or {}).get('health', row['health'])
        if revision:
            revision_lyrics(revision, root)
        if revision and revision.get('evidence'):
            rows = revision['evidence']
            scores = [e['score'] for e in rows if e.get('score') is not None]
            q = {**q, 'total_rows':len(rows),
                 'alignment_median_model_score':statistics.median(scores) if scores else 0,
                 'higher_confidence_rows':[e['row'] for e in rows if (e.get('score') or 0)>=.75
                                           and e.get('disagreement') is not None and e['disagreement']<=1
                                           and e['end']-e['start']>=.35],
                 'lower_confidence_rows':[e['row'] for e in rows if (e.get('score') or 0)<.6
                                          or (e.get('disagreement') or 0)>2]}
        if revision:
            for f in files:
                f['previous_release'] = True
            files[:0] = [video_asset(v, 'updated' if str(v['file']).endswith('.mp4') else 'soft',
                              revision=revision['revision'],
                              timeline_offset=revision.get('timeline_offset', 0)) for v in revision['files']]
        files.extend(f for f in {f['id']: f for f in retained}.values() if f['id'] not in {v['id'] for v in files})
        base = root / row['project'] / row['track']
        supporting = [asset(base / n, 'subtitle' if n.endswith(('.ass', '.srt', '.lrc')) else 'report')
                      for n in ('normal.ass', 'normal.srt', 'bilingual.ass', 'bilingual.srt',
                                'word-timing.lrc', 'track-report.json', 'translation-review.json')
                      if (base / n).is_file()]
        if (base.parent/'index.html').is_file():
            supporting.append(asset(base.parent/'index.html','report',label='整场制作与复核报告'))
        if review:
            folder = root / 'reviewed-samples' / row['project'] / row['track']
            for n in ('bilingual-reviewed.ass', 'bilingual-reviewed.srt', 'translation-changes.json'):
                if (folder / n).is_file():
                    supporting.insert(0, asset(folder / n, 'report' if n.endswith('.json') else 'subtitle', reviewed=True))
        if revision:
            supporting[:0] = [asset(p, 'report' if str(p).endswith(('.json', '.html')) else 'subtitle')
                              for p in revision.get('supporting', [])]
        repair = repairs.get(key)
        if repair:
            repair = {**repair, 'asset':asset(repair['report'],'report',label='自动修复逐行报告')}
            current_supporting = {Path(p).resolve() for p in (revision or {}).get('supporting', [])}
            repair['previous_revision'] = bool(revision) and Path(repair['report']).resolve() not in current_supporting
            supporting.insert(0,repair['asset'])
        experiment = None
        if key in ab:
            case = ab[key]
            experiment = dict(id=case['id'], purpose=case['purpose'], start=case['start'],
                              duration=case['duration'], files=[
                                  asset(root / 'model-comparison' / case['id'] / (v + '.mp4'), 'experiment', label=v)
                                  for v in EXPERIMENTS])
        # Prefer a browser-playable vocal version from the current release.
        default = next((f for f in files if f['exists'] and f['kind'] == 'reviewed' and f['variant'] == 3),
                       next((f for f in files if f['exists'] and f['kind'] == 'hard' and f['variant'] == 3),
                            next((f for f in files if f['exists'] and f['kind'] == 'soft' and f['variant'] == 1), files[0])))
        if revision:
            default = next((f for f in files if f.get('revision') == revision['revision'] and f['variant'] == 3
                            and f['exists'] and f['kind'] == 'updated'),
                           next(f for f in files if f.get('revision') == revision['revision'] and f['variant'] == 1))
        tracks.append(dict(
            id='/'.join(key), project=row['project'], concert=project_labels[row['project']],
            track=row['track'], title=row['track'].partition('_')[2] or row['track'],
            number=row['track'].partition('_')[0], grade=grade({**row,'quality':q,'acceptance':acceptance}),
            score=q['alignment_median_model_score'], total=q['total_rows'],
            high_rows=q['higher_confidence_rows'], low_rows=q['lower_confidence_rows'],
            translation_flags=q['translation_low_rows'], acceptance=acceptance,
            health=health, evidence=evidence[key], files=files, supporting=supporting,
            baseline_acceptance=row['acceptance'], baseline_health=row['health'],
            baseline_quality=baseline_quality,
            reviewed_rows=(review or {}).get('translation_rows_reviewed', 0),
            translation_notes=(review or {}).get('remaining_notes', {}),
            experiment=experiment, revision=revision, revision_history=historical,
            repair=repair, audit=audit.get('/'.join(key)),
            default_file=default['id'], duration=default['duration']))

    media = list({f['path']: f for t in tracks for f in t['files'] if f['exists']}.values())
    experiments = [f for t in tracks if t['experiment'] for f in t['experiment']['files'] if f['exists']]
    return dict(
        updated=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
        root=str(root), projects=project_labels, variants=VARIANTS, tracks=tracks,
        revision_report=(url_prefix+'/'+quote(report.relative_to(root).as_posix(),safe='/')) if report.is_file() else None,
        summary=dict(tracks=len(tracks), grades=dict(Counter(t['grade'] for t in tracks)),
                     files=len(media), soft=sum(f['kind'] == 'soft' for f in media),
                     hard=sum(f['kind'] != 'soft' for f in media),
                     reviewed=sum(f['kind'] == 'reviewed' for f in media),
                     experiments=len(experiments), bytes=sum(f['bytes'] for f in media)),
        diagnostics=diagnostics), allowed


def reveal_in_explorer(path):
    """Select a Unicode file with the Windows Shell API (no shell command parsing).

    https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shopenfolderandselectitems
    """
    import ctypes
    import os
    if os.name != 'nt':
        raise OSError('在资源管理器中显示文件仅支持 Windows')
    ole = ctypes.OleDLL('ole32')
    shell = ctypes.OleDLL('shell32')
    ole.CoInitialize.argtypes = [ctypes.c_void_p]
    ole.CoInitialize.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    shell.SHParseDisplayName.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_void_p]
    shell.SHParseDisplayName.restype = ctypes.c_long
    shell.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    shell.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    ole.CoInitialize(None)
    pidl = ctypes.c_void_p()
    try:
        shell.SHParseDisplayName(str(path), None, ctypes.byref(pidl), 0, None)
        shell.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
    finally:
        if pidl:
            ole.CoTaskMemFree(pidl)
        ole.CoUninitialize()
