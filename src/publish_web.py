"""Local publish-preparation workspace: approved candidates, preflight and packages.

The page never logs in to a platform and never uploads. It only builds a local
Bilibili/Douyin package from a human-approved video and records when the user
marks it as submitted.
"""
from __future__ import annotations

import json
import secrets
import threading
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

import publish_export as publisher
import review_web as review_module
from http_support import reject_post, serve_file

TOKEN = secrets.token_urlsafe(32)
ASSETS = Path(__file__).resolve().parent / 'webui_assets/publish'
ASSET_NAMES = {'index.html', 'publish.css', 'publish.js'}
PACKAGE_FILES = {'video.mp4', 'cover.jpg', 'publish.json', 'upload-info.txt', 'sha256.txt'}
LOCK = threading.RLock()
JOBS = {}
CANCELS = {}


def _now():
    return publisher._now()


def _segments(track_id):
    project, _, track = str(track_id).partition('/')
    return project, track


def _file_url(track_id, platform, name):
    project, track = _segments(track_id)
    return f'/publish-files/{quote(project, safe="")}/{quote(track, safe="")}/{quote(platform, safe="")}/{quote(name, safe="")}'


def _public_package(track_id, platform, record):
    result = dict(record)
    result['files'] = {name: _file_url(track_id, platform, name) for name in
                       (record.get('files') or {}) if name in PACKAGE_FILES}
    return result


def _public_state(track_id, root):
    state = publisher.load_state(track_id, root)
    packages = {platform: _public_package(track_id, platform, record)
                for platform, record in state['packages'].items()}
    return dict(packages=packages, history=state['history'][-40:])


def _settings():
    return publisher.load_settings(publisher.PUBLISH_ROOT)


def _candidates():
    data, _allowed = review_module.catalog()
    review_module.REVIEWS.attach(data)
    return publisher.candidates(data)


def _candidate(track_id):
    items, _skipped = _candidates()
    candidate = next((item for item in items if item['id'] == str(track_id)), None)
    if not candidate:
        raise ValueError('曲目不在“已审批通过”列表中；请先在成品复核中通过并确认文件未变化')
    return candidate


def library():
    items, skipped = _candidates()
    saved = _settings()['tracks']
    for item in items:
        settings = publisher.normalize_settings(item, saved.get(item['id']))
        item['settings'] = settings
        item['state'] = _public_state(item['id'], publisher.PUBLISH_ROOT)
    return dict(token=TOKEN, platforms=publisher.platform_specs(),
                platform_order=publisher.PLATFORM_ORDER, tracks=items, skipped=skipped,
                root=str(publisher.PUBLISH_ROOT), updated=_now(),
                content_warning=publisher.CONTENT_WARNING)


def _platforms(value):
    if not isinstance(value, list) or not value:
        raise ValueError('请选择至少一个发布平台')
    unknown = [item for item in value if item not in publisher.PLATFORMS]
    if unknown:
        raise ValueError('包含未知发布平台')
    return [item for item in publisher.PLATFORM_ORDER if item in value]


def _plan(candidate, settings, platforms):
    return publisher.preflight(candidate, settings, platforms, publisher.PUBLISH_ROOT)


def _progress(job_id, percent, stage):
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            job['progress'] = max(job.get('progress', 0), int(percent))
            job['stage'] = str(stage)[:120]


def _start(candidate, settings, platforms):
    job_id = uuid.uuid4().hex
    cancel = threading.Event()
    with LOCK:
        for job in JOBS.values():
            if job['track_id'] == candidate['id'] and job['state'] == 'running':
                raise ValueError('这首歌已有发布包正在生成，请等待完成或取消')
        job = dict(job=job_id, track_id=candidate['id'], state='running', progress=0,
                   stage='准备', results=[], error=None, started=_now(), finished=None)
        JOBS[job_id] = job
        CANCELS[job_id] = cancel
    root = publisher.PUBLISH_ROOT

    def target():
        try:
            results = publisher.generate(
                candidate, settings, platforms, root=root,
                progress=lambda percent, stage: _progress(job_id, percent, stage),
                cancel=cancel)
            with LOCK:
                job['state'] = 'done'
                job['progress'] = 100
                job['stage'] = '完成'
                job['results'] = [dict(platform=result['platform'],
                                       package=result['package'],
                                       files={name: _file_url(candidate['id'], result['platform'], name)
                                              for name in result['files']},
                                       warnings=result['warnings'], generated_at=result['generated_at'],
                                       video=result['video'], cover=result['cover'])
                                  for result in results]
        except RuntimeError as exc:
            with LOCK:
                job['state'] = 'cancelled'
                job['error'] = str(exc)
        except Exception as exc:  # noqa: BLE001
            with LOCK:
                job['state'] = 'error'
                job['error'] = str(exc)
        finally:
            with LOCK:
                job['finished'] = _now()
                CANCELS.pop(job_id, None)

    threading.Thread(target=target, daemon=True).start()
    return dict(job=job_id)


def _job(job_id):
    with LOCK:
        job = JOBS.get(str(job_id))
        if not job:
            raise KeyError('发布任务不存在或服务已重启')
        return json.loads(json.dumps(job))


def _serve_asset(handler, name):
    if name not in ASSET_NAMES:
        return handler._json({'error': '资源不存在'}, 404)
    content_type = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
    return handler._send(200, content_type + '; charset=utf-8', (ASSETS / name).read_bytes(),
                         {'X-Frame-Options': 'SAMEORIGIN', 'X-Content-Type-Options': 'nosniff'})


def dispatch_get(handler, route, query=None):
    query = query or {}
    if route in ('/publish', '/publish/'):
        return _serve_asset(handler, 'index.html')
    if route.startswith('/publish-assets/'):
        return _serve_asset(handler, unquote(route.removeprefix('/publish-assets/')))
    if route.startswith('/publish-files/'):
        parts = [unquote(part) for part in route.removeprefix('/publish-files/').split('/')]
        if len(parts) != 4 or parts[3] not in PACKAGE_FILES or parts[2] not in publisher.PLATFORMS:
            return handler._json({'error': '文件不存在'}, 404)
        project, track, platform, name = parts
        root = Path(publisher.PUBLISH_ROOT).resolve()
        path = (root / project / track / platform / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return handler._json({'error': '文件不存在'}, 404)
        return serve_file(handler, path)
    if route == '/api/publish/library':
        return handler._json(library())
    if route == '/api/publish/job':
        try:
            return handler._json(_job((query.get('job') or [''])[0]))
        except KeyError as exc:
            return handler._json({'error': str(exc)}, 404)
    if route == '/api/publish/history':
        track_id = (query.get('track') or [''])[0]
        if not track_id:
            return handler._json({'error': '缺少曲目'}, 400)
        try:
            state = publisher.load_state(track_id, publisher.PUBLISH_ROOT)
            return handler._json(dict(track_id=track_id, history=state['history'],
                                      packages=state['packages']))
        except (ValueError, OSError) as exc:
            return handler._json({'error': str(exc)}, 404)
    return handler._json({'error': '未知发布操作'}, 404)


def dispatch_post(handler, route):
    allowed = {'/api/publish/settings', '/api/publish/preflight', '/api/publish/start',
               '/api/publish/cancel', '/api/publish/mark'}
    if route not in allowed:
        return reject_post(handler, {'error': '未知发布操作'}, 404)
    if not secrets.compare_digest(handler.headers.get('X-Publish-Token', ''), TOKEN):
        return reject_post(handler, {'error': '页面已过期，请刷新发布页'}, 403)
    if handler.headers.get('Transfer-Encoding'):
        return reject_post(handler, {'error': '不支持分块请求'}, 400)
    try:
        length = int(handler.headers.get('Content-Length') or 0)
    except ValueError:
        length = 0
    if not 0 < length <= 262144:
        return reject_post(handler, {'error': '请求过大或为空'}, 413)
    try:
        payload = json.loads(handler._body() or b'{}')
    except (ValueError, UnicodeDecodeError):
        return handler._json({'error': '请求不是有效 JSON'}, 400)
    if not isinstance(payload, dict):
        return handler._json({'error': '需要 JSON 对象'}, 400)
    try:
        if route == '/api/publish/settings':
            track_id = payload.get('track_id')
            candidate = _candidate(track_id)
            settings = payload.get('settings')
            if not isinstance(settings, dict):
                raise ValueError('发布设置必须是对象')
            normalized = publisher.normalize_settings(candidate, settings)
            publisher.save_settings(candidate['id'], normalized, publisher.PUBLISH_ROOT)
            return handler._json({'ok': True, 'settings': normalized})
        if route == '/api/publish/preflight':
            candidate = _candidate(payload.get('track_id'))
            platforms = _platforms(payload.get('platforms'))
            plan = _plan(candidate, payload.get('settings') or {}, platforms)
            return handler._json(plan)
        if route == '/api/publish/start':
            candidate = _candidate(payload.get('track_id'))
            platforms = _platforms(payload.get('platforms'))
            settings = payload.get('settings') or {}
            plan = _plan(candidate, settings, platforms)
            if plan['errors'] or any(item['errors'] for item in plan['platforms'].values()):
                return handler._json({'error': '预检未通过，请先处理错误', 'plan': plan}, 400)
            publisher.save_settings(candidate['id'], plan['settings'], publisher.PUBLISH_ROOT)
            return handler._json(_start(candidate, plan['settings'], platforms))
        if route == '/api/publish/cancel':
            job_id = str(payload.get('job') or '')
            with LOCK:
                job = JOBS.get(job_id)
                if not job:
                    return handler._json({'error': '发布任务不存在'}, 404)
                cancel = CANCELS.get(job_id)
                if cancel:
                    cancel.set()
                job['stage'] = '正在取消'
            return handler._json({'ok': True, 'job': job_id})
        data = publisher.mark_submitted(
            str(payload.get('track_id') or ''), str(payload.get('platform') or ''),
            url=str(payload.get('url') or ''), bvid=str(payload.get('bvid') or ''),
            note=str(payload.get('note') or ''), root=publisher.PUBLISH_ROOT)
        return handler._json({'ok': True, 'record': data})
    except (ValueError, OSError) as exc:
        return handler._json({'error': str(exc)}, 400)
