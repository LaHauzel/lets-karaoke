"""Independent concert jobs, persistence and HTTP routes for the local WebUI."""
from __future__ import annotations
import copy
import json
import importlib.util
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

from concert_splitter import (Cancelled, probe, extract_features, extract_sound_scores,
                              suggest, validate_segments, export_segments, verify_source,
                              verify_export_plan, cancel_running_processes)
from http_support import request_allowed, serve_file as send_media_file

ROOT = Path(__file__).resolve().parents[1] / 'out' / 'concert'
LOCK = threading.RLock()
JOBS = {}
ACTIVE = set()
CANCEL = set()


def shutdown(timeout=5):
    """Cancel workers and reap their media processes before closing the server."""
    with LOCK:
        CANCEL.update(ACTIVE)
    cancel_running_processes()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with LOCK:
            if not ACTIVE:
                return True
        time.sleep(.05)
    return False


def directory(identifier):
    if not re.fullmatch(r'[0-9a-f]{16}', str(identifier)):
        raise ValueError('无效任务编号')
    return ROOT / identifier


def persist(job):
    with LOCK:
        dest = directory(job['id']) / 'concert.json'
        temp = dest.with_name(f'concert-{uuid.uuid4().hex}.tmp')
        try:
            temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(dest)
        finally:
            temp.unlink(missing_ok=True)


def load_record(identifier):
    """Load one physical record without following its migration alias."""
    directory(identifier)
    if identifier not in JOBS:
        path = directory(identifier) / 'concert.json'
        if not path.exists():
            raise FileNotFoundError('任务不存在')
        job = json.loads(path.read_text(encoding='utf-8'))
        if job.get('edited') and not job.get('legacy_versions_migrated'):
            current = next((v for v in job.get('analysis_versions', [])
                            if v.get('version') == job.get('analysis_version')), None)
            if current is not None and current.get('segments') != job.get('segments'):
                # Older releases saved edits only in the root record.
                current.update(segments=copy.deepcopy(job.get('segments', [])), edited=True,
                               revision=int(job.get('revision', 0)))
        if job['state'] in {'analyzing', 'exporting'} and identifier not in ACTIVE:
            job.update(state='interrupted', message='服务曾中断；可继续未完成导出或重新分析。')
            for export in job.get('exports', []):
                if export.get('state') == 'exporting':
                    export['state'] = 'interrupted'
        JOBS[identifier] = job
    return JOBS[identifier]


def get_job(identifier):
    with LOCK:
        seen = set()
        while True:
            directory(identifier)
            if identifier in seen:
                raise ValueError('任务记录引用循环，请恢复备份')
            seen.add(identifier)
            job = load_record(identifier)
            target = job.get('merged_into')
            if not target or target == identifier:
                if job.get('legacy_versions_migrated'):
                    current = next((v for v in job.get('analysis_versions', [])
                                    if v.get('version') == job.get('analysis_version')), None)
                    if current is not None:
                        job.update({k: copy.deepcopy(v) for k, v in current.items()
                                    if k not in {'version', 'created', 'origin_id', 'origin_version'}})
                        job['revision'] = int(current.get('revision', 0))
                        job['edited'] = bool(current.get('edited'))
                return job
            identifier = target


def legacy_version_map(alias, target):
    """Recover pre-map aliases from immutable analysis metadata, never guess."""
    old_versions = alias.get('analysis_versions') or [version_payload(alias, 1)]
    fields = ('created', 'sensitivity', 'min_length', 'candidates', 'waveform',
              'speech_ranges', 'method', 'notice', 'source_identity')
    mapping, used = {}, set()
    for old in old_versions:
        available = [v for v in target.get('analysis_versions', []) if int(v['version']) not in used]
        matches = [v for v in available if v.get('origin_id') == alias['id'] and
                   v.get('origin_version') == old['version']]
        if not matches:
            matches = [v for v in available if all(v.get(k) == old.get(k) for k in fields)]
        if len(matches) != 1:
            raise ValueError('旧记录版本映射无法唯一确认，请从历史列表重新打开合并记录')
        number = int(matches[0]['version'])
        mapping[str(old['version'])] = number
        used.add(number)
    return mapping


def resolve_job_version(identifier, version=None):
    """Resolve both record identity and its original version through aliases."""
    with LOCK:
        seen, wanted = set(), int(version) if version is not None else None
        while True:
            if identifier in seen:
                raise ValueError('任务记录引用循环，请恢复备份')
            seen.add(identifier)
            record = load_record(identifier)
            target_id = record.get('merged_into')
            if not target_id or target_id == identifier:
                return record, wanted
            if wanted is None:
                wanted = int(record.get('analysis_version', 1))
            mapping = record.get('merged_versions')
            if not mapping:
                mapping = legacy_version_map(record, load_record(target_id))
                record['merged_versions'] = mapping
                persist(record)
            if str(wanted) not in mapping:
                raise ValueError('版本不存在')
            wanted = int(mapping[str(wanted)])
            identifier = target_id


def active_ids(identifier):
    canonical = get_job(identifier)['id']
    return {value for value in ACTIVE if get_job(value)['id'] == canonical}


def export_directory(job, export):
    owner = export.get('owner_id')
    if not owner and export.get('path'):
        legacy = Path(export['path']).resolve()
        if legacy.is_relative_to(ROOT.resolve()) and legacy.name == export['id']:
            owner = legacy.parent.name
    owner = owner or job['id']
    if get_job(owner)['id'] != job['id'] or not re.fullmatch(r'export-[a-f0-9]+', export['id']):
        raise ValueError('无效导出文件归属')
    folder = directory(owner) / export['id']
    if not folder.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError('导出目录超出工作区')
    return folder


def snapshot(identifier):
    with LOCK:
        return copy.deepcopy(get_job(identifier))


def update(identifier, **values):
    with LOCK:
        job = get_job(identifier)
        job.update(values)
        persist(job)


def start_worker(identifier, target, args):
    with LOCK:
        ACTIVE.add(identifier)
        try:
            threading.Thread(target=target, args=args, daemon=True).start()
        except BaseException as exc:
            ACTIVE.discard(identifier)
            CANCEL.discard(identifier)
            job = get_job(identifier)
            job.update(state='error', message=f'无法启动后台处理：{exc}')
            for export in job.get('exports', []):
                if export.get('state') == 'exporting':
                    export['state'] = 'error'
            try:
                persist(job)
            except OSError:
                pass
            raise


def version_payload(job, number=None):
    return {
        'version': number if number is not None else job.get('analysis_version', 1),
        'created': job.get('updated', job.get('created', time.time())),
        'sensitivity': job.get('sensitivity', 'balanced'),
        'min_length': job.get('min_length', 120),
        'candidates': copy.deepcopy(job.get('candidates', [])),
        'segments': copy.deepcopy(job.get('segments', [])),
        'waveform': copy.deepcopy(job.get('waveform', [])),
        'speech_ranges': copy.deepcopy(job.get('speech_ranges', [])),
        'method': job.get('method'),
        'notice': job.get('notice'),
        'source_identity': copy.deepcopy(job.get('source_identity')),
        'edited': bool(job.get('edited')), 'revision': int(job.get('revision', 0)),
    }


def append_version(job):
    versions = job.setdefault('analysis_versions', [])
    number = int(job.get('analysis_version') or (len(versions) + 1))
    versions[:] = [v for v in versions if int(v.get('version', 0)) != number]
    versions.append(version_payload(job, number))
    versions.sort(key=lambda v: int(v.get('version', 0)))
    job['analysis_version'] = number
    job['version_count'] = len(versions)


def migrate_legacy_records():
    """Coalesce older re-analysis jobs made before versioned records existed."""
    with LOCK:
        return _migrate_legacy_records_locked()


def _migrate_legacy_records_locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    active_canonical = {get_job(identifier)['id'] for identifier in ACTIVE}
    groups = {}
    for path in ROOT.glob('*/concert.json'):
        try:
            raw = copy.deepcopy(JOBS.get(path.parent.name) or json.loads(path.read_text(encoding='utf-8')))
            if raw.get('state') in {'analyzing', 'exporting'} and raw.get('id') not in ACTIVE:
                raw.update(state='interrupted', message='服务曾中断；可继续未完成导出或重新分析。')
                for export in raw.get('exports', []):
                    if export.get('state') == 'exporting':
                        export['state'] = 'interrupted'
            identity = json.dumps(raw.get('source_identity'), sort_keys=True)
            groups.setdefault((str(Path(raw.get('source', '')).resolve()).casefold(), identity), []).append(raw)
        except (OSError, ValueError, TypeError):
            continue
    for _, all_records in groups.items():
        # Never change identity/storage of a live worker, including aliases.
        if any(r.get('id') in ACTIVE or r.get('id') in active_canonical or
               r.get('merged_into') in active_canonical for r in all_records):
            continue
        bases = [r for r in all_records if not r.get('merged_into')]
        if len(bases) < 2:
            continue
        bases.sort(key=lambda x: (float(x.get('created', 0)), x.get('id', '')))
        canonical = bases[0]
        # A canonical record can receive new duplicate jobs created by an old
        # cached page after the first migration. Merge those incrementally.
        if canonical.get('legacy_versions_migrated'):
            new_records = bases[1:]
            if not new_records:
                continue
            records = [canonical] + new_records
            versions = copy.deepcopy(canonical.get('analysis_versions') or [])
            version_counter = max((int(v.get('version', 0)) for v in versions), default=0)
        else:
            records = bases + [r for r in all_records if r.get('merged_into') == canonical.get('id')]
            records.sort(key=lambda x: (float(x.get('created', 0)), x.get('id', '')))
            versions = []
            version_counter = 0
        exports = []
        version_maps = {}
        for raw in sorted(all_records, key=lambda record: bool(record.get('merged_into'))):
            for value in raw.get('exports') or []:
                value = copy.deepcopy(value)
                if not value.get('owner_id'):
                    legacy = Path(value.get('path') or '').resolve()
                    value['owner_id'] = (legacy.parent.name if legacy.is_relative_to(ROOT.resolve())
                                         and legacy.name == value.get('id') else raw['id'])
                exports.append((raw['id'], value))
        for raw in records[1:] if canonical.get('legacy_versions_migrated') else records:
            old_versions = copy.deepcopy(raw.get('analysis_versions') or [version_payload(raw, 1)])
            if raw.get('edited'):
                for value in old_versions:
                    if value.get('version') == raw.get('analysis_version'):
                        value.update(segments=copy.deepcopy(raw.get('segments', [])), edited=True,
                                     revision=int(raw.get('revision', 0)))
            for value in old_versions:
                version_counter += 1
                value = copy.deepcopy(value)
                version_maps.setdefault(raw['id'], {})[value.get('version', 1)] = version_counter
                value.setdefault('origin_id', raw['id'])
                value.setdefault('origin_version', value.get('version', 1))
                value['version'] = version_counter
                versions.append(value)
            if float(raw.get('created', 0)) >= float(canonical.get('created', 0)):
                for key in ('state', 'progress', 'message', 'min_length', 'sensitivity', 'candidates',
                            'segments', 'waveform', 'speech_ranges', 'method', 'notice', 'updated',
                            'revision', 'edited'):
                    if key in raw:
                        canonical[key] = raw[key]
        dedup = {}
        for value in versions:
            dedup[int(value.get('version', len(dedup) + 1))] = value
        canonical['analysis_versions'] = [dedup[n] for n in sorted(dedup)]
        canonical['analysis_version'] = max(dedup) if dedup else 1
        if dedup:
            latest = dedup[canonical['analysis_version']]
            canonical.update({k: copy.deepcopy(v) for k, v in latest.items()
                              if k not in {'version', 'created', 'origin_id', 'origin_version'}})
            canonical['revision'] = int(latest.get('revision', 0))
            canonical['edited'] = bool(latest.get('edited'))
        canonical['version_count'] = len(canonical['analysis_versions'])
        canonical['legacy_versions_migrated'] = True
        if canonical.get('state') in {'ready', 'done'}:
            canonical['message'] = f'已完成分析版本 v{canonical["analysis_version"]}，可在版本菜单中切换。'
        seen = set()
        for record_id, value in exports:
            mapping = version_maps.get(record_id, {})
            if value.get('version') in mapping:
                value['version'] = mapping[value['version']]
        canonical['exports'] = [x for _, x in exports if not (x.get('id') in seen or seen.add(x.get('id')))]
        JOBS[canonical['id']] = canonical
        persist(canonical)
        for duplicate in records[1:]:
            duplicate['merged_into'] = canonical['id']
            duplicate['merged_versions'] = {str(k): v for k, v in version_maps.get(duplicate['id'], {}).items()}
            duplicate['message'] = f'已合并到版本记录 {canonical["id"]}'
            JOBS[duplicate['id']] = duplicate
            persist(duplicate)


def job_view(job, version=None):
    if version is None:
        current = next((v for v in job.get('analysis_versions', [])
                        if int(v.get('version', 0)) == int(job.get('analysis_version', 0))), None)
        if current is None:
            return copy.deepcopy(job)
        version = current['version']
    try:
        wanted = int(version)
    except (TypeError, ValueError):
        raise ValueError('无效版本号')
    selected = next((v for v in job.get('analysis_versions', []) if int(v.get('version', 0)) == wanted), None)
    if selected is None:
        raise ValueError('版本不存在')
    view = copy.deepcopy(job)
    view.update({k: copy.deepcopy(v) for k, v in selected.items()
                 if k not in {'version', 'created', 'origin_id', 'origin_version'}})
    view['analysis_version'] = wanted
    view['selected_version'] = wanted
    view['revision'] = int(selected.get('revision', 0))
    view['source_identity'] = copy.deepcopy(selected.get('source_identity'))
    return view


def save_version_segments(job, segments, version=None, revision=None):
    wanted = int(version if version is not None else job.get('analysis_version', 1))
    versions = job.setdefault('analysis_versions', [])
    selected = next((v for v in versions if int(v['version']) == wanted), None)
    if selected is None:
        if versions or wanted != job.get('analysis_version', 1):
            raise ValueError('版本不存在')
        append_version(job)
        selected = versions[-1]
    if revision is not None and int(revision) != int(selected.get('revision', 0)):
        raise ValueError('该版本已在另一页面更新，请重新载入后再保存')
    selected.update(segments=copy.deepcopy(segments), edited=True, updated=time.time(),
                    revision=int(selected.get('revision', 0)) + 1)
    if wanted == job.get('analysis_version'):
        job.update(segments=copy.deepcopy(segments), edited=True, updated=selected['updated'],
                   revision=selected['revision'])
    persist(job)
    return wanted


def restore_completed_version(identifier, state, message):
    with LOCK:
        job = get_job(identifier)
        versions = job.get('analysis_versions') or []
        if versions:
            latest = max(versions, key=lambda v: int(v['version']))
            job.update({k: copy.deepcopy(v) for k, v in latest.items()
                        if k not in {'version', 'created', 'origin_id', 'origin_version'}})
            job['analysis_version'] = latest['version']
        job.update(state=state, message=message)
        persist(job)


def run_analysis(identifier):
    try:
        job = snapshot(identifier)
        import numpy as np
        from concert_sound_classifier import classifier_key
        identity = verify_source(job['source'], job.get('source_identity'))
        model_key = classifier_key()
        vad_available = importlib.util.find_spec('webrtcvad') is not None
        cache = directory(identifier) / 'features.npz'
        energy = spectra = speech_activity = sound_scores = None
        sound_cached = False
        try:
            with np.load(cache, allow_pickle=False) as values:
                meta = json.loads(str(values['metadata'].item()))
                if meta.get('format') == 2 and meta.get('source_identity') == identity:
                    if meta.get('vad_available') == vad_available:
                        energy, spectra = values['energy'].copy(), values['spectra'].copy()
                        if (energy.ndim != 1 or not len(energy) or spectra.ndim != 2 or
                                len(spectra) != len(energy) or not np.all(np.isfinite(energy)) or
                                not np.all(np.isfinite(spectra))):
                            energy = spectra = None
                        speech_activity = values['speech_activity'].copy() if 'speech_activity' in values else None
                    if meta.get('classifier_key') == model_key:
                        sound_cached = True
                        if 'sound_speech' in values:
                            sound_scores = {'speech': values['sound_speech'].copy(),
                                            'music': values['sound_music'].copy(),
                                            'times': values['sound_times'].copy(),
                                            'hop': .48, 'window': .975}
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            pass
        reused = energy is not None and sound_cached
        if energy is None:
            energy, spectra, speech_activity = extract_features(job['source'], job['duration'],
                lambda p, m: update(identifier, progress=p, message=m), lambda: identifier in CANCEL)
        else:
            update(identifier, progress=.85, message='正在复用已缓存音频特征')
        if identifier in CANCEL:
            raise Cancelled()
        classifier_warning = None
        if not sound_cached:
            try:
                sound_scores = extract_sound_scores(job['source'], job['duration'],
                    lambda p, m: update(identifier, progress=p, message=m), lambda: identifier in CANCEL)
            except ValueError as exc:
                # Optional classification must not discard valid boundary work.
                classifier_warning = str(exc)
                model_key = None  # Retry after model/runtime repair instead of caching failure.
        if identifier in CANCEL:
            raise Cancelled()
        verify_source(job['source'], identity)
        arrays = {'energy': energy, 'spectra': spectra,
                  'metadata': json.dumps({'format': 2, 'source_identity': identity,
                                         'vad_available': vad_available, 'classifier_key': model_key})}
        if speech_activity is not None:
            arrays['speech_activity'] = speech_activity
        if sound_scores is not None:
            arrays.update(sound_speech=sound_scores['speech'], sound_music=sound_scores['music'],
                          sound_times=sound_scores['times'])
        temporary = cache.with_suffix('.tmp')
        try:
            with temporary.open('wb') as stream:
                np.savez_compressed(stream, **arrays)
            temporary.replace(cache)
        finally:
            temporary.unlink(missing_ok=True)
        result = suggest(energy, spectra, job['duration'], job['min_length'], job['sensitivity'],
                         speech_activity=speech_activity, sound_scores=sound_scores)
        if classifier_warning:
            result['notice'] += ' 本次讲话分类不可用：' + classifier_warning
        with LOCK:
            current = get_job(identifier)
            current.update(**result, state='ready', progress=1, updated=time.time(), source_identity=identity,
                           cache_reused=reused, message=f'分析完成：{len(result["candidates"])} 个候选边界，请试听复核。')
            append_version(current)
            persist(current)
    except Cancelled:
        restore_completed_version(identifier, 'cancelled', '分析已取消；已保存的版本和调整仍可继续使用。')
    except Exception as exc:
        restore_completed_version(identifier, 'error', str(exc))
    finally:
        with LOCK:
            ACTIVE.discard(identifier)
            CANCEL.discard(identifier)


def create_analysis(info, min_length, sensitivity):
    identifier = uuid.uuid4().hex[:16]
    directory(identifier).mkdir(parents=True)
    job = {**info, 'id': identifier, 'created': time.time(), 'min_length': min_length,
           'sensitivity': sensitivity, 'state': 'analyzing', 'progress': 0,
           'message': '正在读取音频', 'segments': [], 'exports': [],
           'analysis_versions': [], 'analysis_version': 1, 'version_count': 0}
    JOBS[identifier] = job
    persist(job)
    start_worker(identifier, run_analysis, (identifier,))
    return identifier


def restart_analysis(identifier, info, min_length, sensitivity):
    job = get_job(identifier)
    identifier = job['id']
    next_version = max((int(v['version']) for v in job.get('analysis_versions', [])), default=0) + 1
    job.update(**info, min_length=min_length, sensitivity=sensitivity, state='analyzing',
               progress=0, message='正在读取音频', segments=[], candidates=[], waveform=[], speech_ranges=[],
               updated=time.time(), selected_version=None, analysis_version=next_version, edited=False, revision=0)
    persist(job)
    start_worker(identifier, run_analysis, (identifier,))
    return identifier


def run_export(identifier, segments, mode, export_id, resume=False):
    def completed(value):
        with LOCK:
            current = get_job(identifier)
            export = next(ex for ex in current['exports'] if ex['id'] == export_id)
            export['files'] = [f for f in export['files'] if f['file'] != value['file']] + [value]
            persist(current)
    try:
        job = snapshot(identifier)
        export = next(ex for ex in job['exports'] if ex['id'] == export_id)
        files = export_segments(job['source'], segments, export_directory(job, export), mode,
            lambda p, m: update(identifier, progress=p, message=m), lambda: identifier in CANCEL,
            completed_files=export['files'] if resume else None, on_complete=completed,
            expected_identity=export.get('source_identity'))
        with LOCK:
            job = get_job(identifier)
            export = next(ex for ex in job['exports'] if ex['id'] == export_id)
            export.update(files=files, state='done')
            job.update(state='done', progress=1, message=f'已导出 {len(files)} 个片段')
            persist(job)
    except Cancelled:
        update(identifier, state='cancelled', message='导出已取消；已完成文件保留在本次导出目录。')
    except Exception as exc:
        update(identifier, state='error', message=str(exc))
    finally:
        with LOCK:
            try:
                current = get_job(identifier)
                export = next(ex for ex in current.get('exports', []) if ex['id'] == export_id)
                export['state'] = current['state']
                persist(current)
            finally:
                ACTIVE.discard(identifier)
                CANCEL.discard(identifier)


def serve_file(handler, path):
    return send_media_file(handler, path, download=parse_qs(urlparse(handler.path).query).get('download') == ['1'])


def dispatch_get(handler, path, query):
    try:
        if path == '/api/concert/jobs':
            migrate_legacy_records()
            records = []
            for file in ROOT.glob('*/concert.json'):
                try:
                    identifier = file.parent.name
                    with LOCK:
                        raw = JOBS.get(identifier) or json.loads(file.read_text(encoding='utf-8'))
                        if raw.get('merged_into'):
                            continue
                        j = snapshot(identifier)
                    records.append({k: j.get(k) for k in ('id', 'name', 'state', 'duration', 'created', 'message', 'analysis_version', 'version_count', 'sensitivity')})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            return handler._json(sorted(records, key=lambda j: -j['created']))
        if path == '/api/concert/job':
            with LOCK:
                job, version = resolve_job_version(query.get('id', [''])[0], query.get('version', [None])[0])
                return handler._json(job_view(copy.deepcopy(job), version))
        if path.startswith('/concert-files/'):
            parts = [unquote(p) for p in path.split('/')[2:]]
            if len(parts) < 2:
                raise ValueError('无效路径')
            job = snapshot(parts[0])
            if parts[1:] == ['source']:
                return serve_file(handler, Path(job['source']))
            # Only published artifacts, never arbitrary paths inside a task.
            if parts[1:] == ['concert.json']:
                return serve_file(handler, directory(job['id'])/'concert.json')
            for export in job.get('exports', []):
                if len(parts) == 3 and parts[1] == export['id'] and parts[2] in ['manifest.json']+[f['file'] for f in export['files']]:
                    folder = export_directory(job, export)
                    target = folder / parts[2]
                    if Path(parts[2]).name != parts[2] or not target.resolve().is_relative_to(folder.resolve()):
                        raise ValueError('无效导出文件名')
                    return serve_file(handler, target)
            raise FileNotFoundError('未找到导出文件')
        return handler._json({'error': 'not found'}, 404)
    except FileNotFoundError as exc:
        return handler._json({'error': str(exc)}, 404)
    except (ValueError, KeyError, TypeError) as exc:
        return handler._json({'error': str(exc)}, 400)


def dispatch_post(handler, path):
    try:
        content_type = handler.headers.get('Content-Type', '')
        content_length = int(handler.headers.get('Content-Length', '0') or 0)
        if content_length > 1024 * 1024:
            handler.close_connection = True
            return handler._json({'error': '请求过大'}, 413)
        # Always consume the request body before returning an origin error.  With
        # HTTP/1.1 keep-alive, leaving JSON unread makes it look like the next
        # GET request starts with '{...GET /', producing a misleading 400 log.
        body = handler._body()
        if not request_allowed(handler):
            return handler._json({'error': '仅允许本地工作台发起操作'}, 403)
        if 'application/json' not in content_type:
            return handler._json({'error': '需要 JSON 请求'}, 415)
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError('请求必须是 JSON 对象')
        if path == '/api/concert/analyze':
            info = probe(data.get('source', ''))
            min_length = float(data.get('min_length', 120))
            sensitivity = data.get('sensitivity', 'balanced')
            import math
            if not math.isfinite(min_length) or not 30 <= min_length <= 1800 or sensitivity not in {'balanced', 'sensitive', 'conservative'}:
                raise ValueError('分析参数无效')
            with LOCK:
                if ACTIVE:
                    return handler._json({'error': '已有切割任务在处理，请完成或取消后再开始'}, 409)
                identifier = create_analysis(info, min_length, sensitivity)
            return handler._json({'id': identifier}, 202)
        if path == '/api/concert/reanalyze':
            with LOCK:
                if ACTIVE:
                    return handler._json({'error': '已有切割任务在处理，请完成或取消后再开始'}, 409)
                record, version = resolve_job_version(str(data.get('id') or ''), data.get('version'))
                old = job_view(record, version)
                min_length = float(data.get('min_length', old.get('min_length', 120)))
                sensitivity = data.get('sensitivity', old.get('sensitivity', 'balanced'))
                import math
                if not math.isfinite(min_length) or not 30 <= min_length <= 1800 or sensitivity not in {'balanced', 'sensitive', 'conservative'}:
                    raise ValueError('分析参数无效')
                verify_source(old['source'], old.get('source_identity'))
                identifier = restart_analysis(old['id'], probe(old['source']), min_length, sensitivity)
            return handler._json({'id': identifier}, 202)
        identifier = data.get('id', '')
        with LOCK:
            job, requested_version = resolve_job_version(identifier, data.get('version')) if path in {
                '/api/concert/save', '/api/concert/export'} else (get_job(identifier), None)
            running = active_ids(job['id'])
            identifier = job['id']
            if path == '/api/concert/cancel':
                CANCEL.update(running)
                return handler._json({'ok': True})
            if path == '/api/concert/delete':
                if running:
                    return handler._json({'error': '任务运行期间不能删除记录'}, 409)
                canonical = get_job(identifier)
                canonical_id = canonical['id']
                removed = []
                for record in ROOT.glob('*/concert.json'):
                    try:
                        raw = json.loads(record.read_text(encoding='utf-8'))
                        belongs = get_job(raw['id'])['id'] == canonical_id
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                    if belongs:
                        folder = record.parent.resolve()
                        if folder.is_relative_to(ROOT.resolve()) and folder != ROOT.resolve():
                            removed.append((raw.get('id'), folder))
                for record_id, folder in removed:
                    shutil.rmtree(folder)
                    JOBS.pop(record_id, None)
                return handler._json({'ok': True, 'removed': [record_id for record_id, _ in removed]})
            if running:
                return handler._json({'error': '任务运行期间不能修改分段'}, 409)
            if path == '/api/concert/resume-export':
                if ACTIVE:
                    return handler._json({'error': '已有切割任务在处理'}, 409)
                export = next((ex for ex in job.get('exports', []) if ex['id'] == data.get('export_id')), None)
                if not export or export.get('state', 'done') == 'done' or not export.get('segments'):
                    raise ValueError('此批次没有可继续的导出')
                if not export.get('source_identity'):
                    raise ValueError('此旧批次未记录原视频指纹，请重新分析并导出')
                verify_source(job['source'], export['source_identity'])
                folder = export_directory(job, export)
                manifest = folder / 'manifest.json'
                if manifest.is_file():
                    saved = json.loads(manifest.read_text(encoding='utf-8'))
                    verify_export_plan(saved, job['source'], export['source_identity'], export['mode'],
                                       [s for s in export['segments'] if s['selected']])
                    for value in saved.get('segments', []):
                        if Path(value['file']).name != value['file']:
                            raise ValueError('导出清单文件名无效')
                    # The worker verifies manifest files before publishing
                    # them via on_complete; do not expose unverified entries.
                pending = folder / 'publication.json'
                if pending.is_file():
                    verify_export_plan(json.loads(pending.read_text(encoding='utf-8')), job['source'],
                                       export['source_identity'], export['mode'],
                                       [s for s in export['segments'] if s['selected']])
                export['state'] = 'exporting'
                job.update(state='exporting', progress=0, message='正在继续未完成片段')
                persist(job)
                start_worker(identifier, run_export, (identifier, copy.deepcopy(export['segments']),
                             export['mode'], export['id'], True))
                return handler._json({'id': identifier, 'version': export.get('version')}, 202)
            if path in {'/api/concert/save', '/api/concert/export'}:
                segments = validate_segments(data.get('segments'), job['duration'])
                if path.endswith('/export'):
                    if ACTIVE:
                        return handler._json({'error': '已有切割任务在处理'}, 409)
                    mode = data.get('mode', 'copy')
                    if mode not in {'copy', 'precise'} or not any(s['selected'] for s in segments):
                        raise ValueError('请选择有效导出模式及至少一个片段')
                    viewed = job_view(job, requested_version)
                    if not viewed.get('source_identity'):
                        raise ValueError('此旧版本未记录原视频指纹，请重新分析后再导出')
                    verify_source(job['source'], viewed['source_identity'])
                version = save_version_segments(job, segments, requested_version, data.get('revision'))
                if path.endswith('/save'):
                    return handler._json(job_view(job, version))
                export_id = 'export-' + uuid.uuid4().hex[:12]
                job.setdefault('exports', []).append({'id': export_id, 'mode': mode, 'files': [],
                    'owner_id': identifier, 'path': str(directory(identifier)/export_id),
                    'source_identity': copy.deepcopy(viewed['source_identity']),
                    'segments': copy.deepcopy(segments), 'version': version, 'state': 'exporting'})
                job.update(state='exporting', progress=0, message='准备导出', current_export=str(directory(identifier)/export_id))
                persist(job)
                start_worker(identifier, run_export, (identifier, copy.deepcopy(segments), mode, export_id))
                return handler._json({'id': identifier, 'version': version}, 202)
        return handler._json({'error': 'not found'}, 404)
    except FileNotFoundError as exc:
        return handler._json({'error': str(exc)}, 404)
    except (ValueError, KeyError, TypeError) as exc:
        return handler._json({'error': str(exc)}, 400)
