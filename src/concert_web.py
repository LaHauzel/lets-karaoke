"""Independent concert jobs, persistence and HTTP routes for the local WebUI."""
from __future__ import annotations
import copy
import json
import mimetypes
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse, quote

from concert_splitter import Cancelled, probe, extract_features, suggest, validate_segments, export_segments

ROOT = Path(__file__).resolve().parents[1] / 'out' / 'concert'
LOCK = threading.RLock()
JOBS = {}
ACTIVE = set()
CANCEL = set()


def directory(identifier):
    if not re.fullmatch(r'[0-9a-f]{16}', str(identifier)):
        raise ValueError('无效任务编号')
    return ROOT / identifier


def persist(job):
    dest = directory(job['id']) / 'concert.json'
    temp = dest.with_suffix('.tmp')
    temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(dest)


def get_job(identifier):
    with LOCK:
        if identifier not in JOBS:
            path = directory(identifier) / 'concert.json'
            if not path.exists():
                raise FileNotFoundError('任务不存在')
            job = json.loads(path.read_text(encoding='utf-8'))
            if job['state'] in {'analyzing', 'exporting'}:
                job.update(state='interrupted', message='服务曾中断；可恢复已保存的分段或重新分析。')
            JOBS[identifier] = job
        job = JOBS[identifier]
        if job.get('merged_into') and job['merged_into'] != identifier:
            return get_job(job['merged_into'])
        return job


def snapshot(identifier):
    with LOCK:
        return copy.deepcopy(get_job(identifier))


def update(identifier, **values):
    with LOCK:
        job = get_job(identifier)
        job.update(values)
        persist(job)


def version_payload(job, number=None):
    return {
        'version': number if number is not None else job.get('analysis_version', 1),
        'created': job.get('updated', job.get('created', time.time())),
        'sensitivity': job.get('sensitivity', 'balanced'),
        'min_length': job.get('min_length', 120),
        'candidates': copy.deepcopy(job.get('candidates', [])),
        'segments': copy.deepcopy(job.get('segments', [])),
        'waveform': copy.deepcopy(job.get('waveform', [])),
        'method': job.get('method'),
        'notice': job.get('notice'),
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
    ROOT.mkdir(parents=True, exist_ok=True)
    groups = {}
    for path in ROOT.glob('*/concert.json'):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
            groups.setdefault(str(Path(raw.get('source', '')).resolve()), []).append(raw)
        except (OSError, ValueError, TypeError):
            continue
    for _, all_records in groups.items():
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
        exports = list(canonical.get('exports') or [])
        for raw in records[1:] if canonical.get('legacy_versions_migrated') else records:
            old_versions = raw.get('analysis_versions') or [version_payload(raw, 1)]
            for value in old_versions:
                version_counter += 1
                value = copy.deepcopy(value)
                value['version'] = version_counter
                versions.append(value)
            exports.extend(raw.get('exports') or [])
            if float(raw.get('created', 0)) >= float(canonical.get('created', 0)):
                for key in ('state', 'progress', 'message', 'min_length', 'sensitivity', 'candidates',
                            'segments', 'waveform', 'method', 'notice', 'updated'):
                    if key in raw:
                        canonical[key] = raw[key]
        dedup = {}
        for value in versions:
            dedup[int(value.get('version', len(dedup) + 1))] = value
        canonical['analysis_versions'] = [dedup[n] for n in sorted(dedup)]
        canonical['analysis_version'] = max(dedup) if dedup else 1
        canonical['version_count'] = len(canonical['analysis_versions'])
        canonical['legacy_versions_migrated'] = True
        if canonical.get('state') in {'ready', 'done'}:
            canonical['message'] = f'已完成分析版本 v{canonical["analysis_version"]}，可在版本菜单中切换。'
        seen = set()
        canonical['exports'] = [x for x in exports if not (x.get('id') in seen or seen.add(x.get('id')))]
        JOBS[canonical['id']] = canonical
        persist(canonical)
        for duplicate in records[1:]:
            duplicate['merged_into'] = canonical['id']
            duplicate['message'] = f'已合并到版本记录 {canonical["id"]}'
            JOBS[duplicate['id']] = duplicate
            persist(duplicate)


def job_view(job, version=None):
    if version is None:
        return copy.deepcopy(job)
    try:
        wanted = int(version)
    except (TypeError, ValueError):
        raise ValueError('无效版本号')
    selected = next((v for v in job.get('analysis_versions', []) if int(v.get('version', 0)) == wanted), None)
    if selected is None:
        raise ValueError('版本不存在')
    view = copy.deepcopy(job)
    view.update({k: copy.deepcopy(v) for k, v in selected.items() if k not in {'version', 'created'}})
    view['analysis_version'] = wanted
    view['selected_version'] = wanted
    return view


def run_analysis(identifier):
    try:
        job = snapshot(identifier)
        import numpy as np
        energy, spectra = extract_features(job['source'], job['duration'],
            lambda p, m: update(identifier, progress=p, message=m), lambda: identifier in CANCEL)
        if identifier in CANCEL:
            raise Cancelled()
        np.savez_compressed(directory(identifier)/'features.npz', energy=energy, spectra=spectra)
        result = suggest(energy, spectra, job['duration'], job['min_length'], job['sensitivity'])
        update(identifier, **result, state='ready', progress=1, updated=time.time(),
               message=f'分析完成：{len(result["candidates"])} 个候选边界，请试听复核。')
        with LOCK:
            append_version(get_job(identifier))
            persist(get_job(identifier))
    except Cancelled:
        update(identifier, state='cancelled', message='分析已取消')
    except Exception as exc:
        update(identifier, state='error', message=str(exc))
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
    ACTIVE.add(identifier)
    threading.Thread(target=run_analysis, args=(identifier,), daemon=True).start()
    return identifier


def restart_analysis(identifier, info, min_length, sensitivity):
    job = get_job(identifier)
    next_version = len(job.get('analysis_versions') or []) + 1
    job.update(**info, min_length=min_length, sensitivity=sensitivity, state='analyzing',
               progress=0, message='正在读取音频', segments=[], candidates=[], waveform=[],
               updated=time.time(), selected_version=None, analysis_version=next_version)
    persist(job)
    ACTIVE.add(identifier)
    threading.Thread(target=run_analysis, args=(identifier,), daemon=True).start()
    return identifier


def run_export(identifier, segments, mode, export_id):
    try:
        job = snapshot(identifier)
        files = export_segments(job['source'], segments, directory(identifier)/export_id, mode,
            lambda p, m: update(identifier, progress=p, message=m), lambda: identifier in CANCEL)
        with LOCK:
            job = get_job(identifier)
            job.setdefault('exports', []).append({'id': export_id, 'mode': mode, 'files': files,
                                                  'path': str(directory(identifier)/export_id)})
            job.update(state='done', progress=1, message=f'已导出 {len(files)} 个片段')
            persist(job)
    except Cancelled:
        update(identifier, state='cancelled', message='导出已取消；已完成文件保留在本次导出目录。')
    except Exception as exc:
        update(identifier, state='error', message=str(exc))
    finally:
        with LOCK:
            ACTIVE.discard(identifier)
            CANCEL.discard(identifier)


def serve_file(handler, path):
    if not path.is_file():
        raise FileNotFoundError('文件不存在或原视频已移动')
    size = path.stat().st_size
    start, end, code = 0, size-1, 200
    value = handler.headers.get('Range')
    if value:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
        if not match or not any(match.groups()):
            return handler._send(416, 'text/plain', b'', {'Content-Range': f'bytes */{size}'})
        a, b = match.groups()
        if a:
            start, end = int(a), min(int(b), size-1) if b else size-1
        else:
            start = max(0, size-int(b))
        if start > end or start >= size:
            return handler._send(416, 'text/plain', b'', {'Content-Range': f'bytes */{size}'})
        code = 206
    handler.send_response(code)
    handler.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
    handler.send_header('Content-Length', str(max(0, end-start+1)))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.send_header('Cache-Control', 'no-store')
    if code == 206:
        handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
    if 'download=1' in handler.path:
        handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
    handler.end_headers()
    try:
        with path.open('rb') as stream:
            stream.seek(start)
            remaining = end-start+1
            while remaining > 0:
                chunk = stream.read(min(1024*1024, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass


def dispatch_get(handler, path, query):
    try:
        if path == '/api/concert/jobs':
            migrate_legacy_records()
            records = []
            for file in ROOT.glob('*/concert.json'):
                try:
                    identifier = file.parent.name
                    raw = JOBS.get(identifier)
                    if raw is None:
                        raw = json.loads(file.read_text(encoding='utf-8'))
                    if raw.get('merged_into'):
                        continue
                    j = snapshot(identifier)
                    if j.get('legacy_versions_migrated') and str(j.get('message', '')).startswith('已合并到版本记录'):
                        j['message'] = f'已完成分析版本 v{j.get("analysis_version", 1)}，可在版本菜单中切换。'
                        JOBS[identifier] = j
                        persist(j)
                    records.append({k: j.get(k) for k in ('id', 'name', 'state', 'duration', 'created', 'message', 'analysis_version', 'version_count', 'sensitivity')})
                except (OSError, ValueError):
                    continue
            return handler._json(sorted(records, key=lambda j: -j['created']))
        if path == '/api/concert/job':
            return handler._json(job_view(snapshot(query.get('id', [''])[0]), query.get('version', [None])[0]))
        if path.startswith('/concert-files/'):
            parts = [unquote(p) for p in path.split('/')[2:]]
            if len(parts) < 2:
                raise ValueError('无效路径')
            job = snapshot(parts[0])
            if parts[1:] == ['source']:
                return serve_file(handler, Path(job['source']))
            # Only published artifacts, never arbitrary paths inside a task.
            if parts[1:] == ['concert.json']:
                return serve_file(handler, directory(parts[0])/'concert.json')
            for export in job.get('exports', []):
                if len(parts) == 3 and parts[1] == export['id'] and parts[2] in ['manifest.json']+[f['file'] for f in export['files']]:
                    return serve_file(handler, directory(parts[0])/parts[1]/parts[2])
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
        origin = handler.headers.get('Origin')
        if origin and origin.lower() not in {'null'}:
            parsed_origin = urlparse(origin)
            origin_host = (parsed_origin.hostname or '').lower()
            request_host = (handler.headers.get('Host') or '').lower()
            request_hostname = request_host.rsplit(':', 1)[0].strip('[]')
            is_loopback = origin_host == 'localhost' or origin_host == '::1' or origin_host.startswith('127.')
            if not is_loopback and origin_host != request_hostname:
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
                old = get_job(str(data.get('id') or ''))
                min_length = float(data.get('min_length', old.get('min_length', 120)))
                sensitivity = data.get('sensitivity', old.get('sensitivity', 'balanced'))
                import math
                if not math.isfinite(min_length) or not 30 <= min_length <= 1800 or sensitivity not in {'balanced', 'sensitive', 'conservative'}:
                    raise ValueError('分析参数无效')
                identifier = restart_analysis(old['id'], probe(old['source']), min_length, sensitivity)
            return handler._json({'id': identifier}, 202)
        identifier = data.get('id', '')
        with LOCK:
            job = get_job(identifier)
            if path == '/api/concert/cancel':
                if identifier in ACTIVE:
                    CANCEL.add(identifier)
                return handler._json({'ok': True})
            if path == '/api/concert/delete':
                if identifier in ACTIVE:
                    return handler._json({'error': '任务运行期间不能删除记录'}, 409)
                canonical = get_job(identifier)
                canonical_id = canonical['id']
                removed = []
                for record in ROOT.glob('*/concert.json'):
                    try:
                        raw = json.loads(record.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        continue
                    if raw.get('id') == canonical_id or raw.get('merged_into') == canonical_id:
                        folder = record.parent.resolve()
                        if folder.is_relative_to(ROOT.resolve()) and folder != ROOT.resolve():
                            shutil.rmtree(folder)
                            removed.append(raw.get('id'))
                            JOBS.pop(raw.get('id'), None)
                return handler._json({'ok': True, 'removed': removed})
            if identifier in ACTIVE:
                return handler._json({'error': '任务运行期间不能修改分段'}, 409)
            if path in {'/api/concert/save', '/api/concert/export'}:
                segments = validate_segments(data.get('segments'), job['duration'])
                if path.endswith('/export'):
                    if ACTIVE:
                        return handler._json({'error': '已有切割任务在处理'}, 409)
                    mode = data.get('mode', 'copy')
                    if mode not in {'copy', 'precise'} or not any(s['selected'] for s in segments):
                        raise ValueError('请选择有效导出模式及至少一个片段')
                job['segments'] = segments
                job['edited'] = True
                persist(job)
                if path.endswith('/save'):
                    return handler._json(copy.deepcopy(job))
                export_id = 'export-' + uuid.uuid4().hex[:12]
                job.update(state='exporting', progress=0, message='准备导出', current_export=str(directory(identifier)/export_id))
                persist(job)
                ACTIVE.add(identifier)
                threading.Thread(target=run_export, args=(identifier, copy.deepcopy(segments), mode, export_id), daemon=True).start()
                return handler._json({'id': identifier}, 202)
        return handler._json({'error': 'not found'}, 404)
    except FileNotFoundError as exc:
        return handler._json({'error': str(exc)}, 404)
    except (ValueError, KeyError, TypeError) as exc:
        return handler._json({'error': str(exc)}, 400)
