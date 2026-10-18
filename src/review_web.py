"""Local delivery library and human review routes for the workspace tab."""
import json
import secrets
import threading
from pathlib import Path
from urllib.parse import unquote

from http_support import reject_post, serve_file
from review_catalog import ROOT, build_catalog, reveal_in_explorer
from review_store import ReviewConflict, ReviewStore

ASSETS = Path(__file__).resolve().parent/'webui_assets/library'
NATIVE_ROOT = Path(__file__).resolve().parents[1]/'out/webui'
REVIEWS = ReviewStore(ROOT.parent/'human-reviews.json')
TOKEN = secrets.token_urlsafe(32)
REVEAL_LOCK = threading.Lock()
ASSET_NAMES = {'index.html', 'library.css', 'library.js', 'human-review.css', 'human-review.js'}


def catalog():
    from review_native import append_native
    data,allowed = build_catalog(ROOT, url_prefix='/review-exports')
    append_native(data,allowed,NATIVE_ROOT)
    from review_workflow import attach_links
    attach_links(data,ROOT,NATIVE_ROOT)
    return data,allowed


def dispatch_get(handler, route):
    if route.startswith('/review-native/'):
        _,allowed = catalog()
        path = allowed.get(route.removeprefix('/review-native/'))
        if not path or not path.resolve().is_relative_to(NATIVE_ROOT.resolve()) or not path.is_file():
            return handler._json({'error':'项目成品不存在'},404)
        return serve_file(handler,path)
    if route == '/api/review/library':
        data, _ = catalog()
        REVIEWS.attach(data)
        data['reveal_token'] = TOKEN
        return handler._json(data)
    if route == '/api/review/reviews/export':
        body = (json.dumps(REVIEWS.read(), ensure_ascii=False, indent=2)+'\n').encode('utf8')
        return handler._send(200, 'application/json; charset=utf-8', body,
                             {'Content-Disposition':'attachment; filename="human-reviews.json"'})
    if route == '/review' or route.startswith('/review-assets/'):
        name = 'index.html' if route == '/review' else route.removeprefix('/review-assets/')
        if name not in ASSET_NAMES:
            return handler._json({'error':'资源不存在'}, 404)
        types = {'.html':'text/html', '.css':'text/css', '.js':'text/javascript'}
        return handler._send(200, types[Path(name).suffix]+'; charset=utf-8', (ASSETS/name).read_bytes(),
                             {'X-Frame-Options':'SAMEORIGIN', 'X-Content-Type-Options':'nosniff'})
    if route.startswith('/review-exports/'):
        path = (ROOT/unquote(route.removeprefix('/review-exports/'))).resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            return handler._json({'error':'成品不存在'}, 404)
        return serve_file(handler, path)
    return handler._json({'error':'未知复核操作'}, 404)


def dispatch_post(handler, route):
    if route not in {'/api/review/reviews', '/api/review/reveal', '/api/review/edit'}:
        return reject_post(handler, {'error':'未知复核操作'}, 404)
    if not secrets.compare_digest(handler.headers.get('X-Library-Token',''), TOKEN):
        return reject_post(handler, {'error':'页面已过期，请刷新成品清单'}, 403)
    if handler.headers.get('Transfer-Encoding'):
        return reject_post(handler, {'error':'不支持分块请求'}, 400)
    length = int(handler.headers.get('Content-Length') or 0)
    if not 0 < length <= 262144:
        return reject_post(handler, {'error':'复核请求过大或为空'}, 413)
    payload = json.loads(handler._body())
    if not isinstance(payload, dict):
        return handler._json({'error':'需要 JSON 对象'}, 400)
    data, allowed = catalog()
    if route == '/api/review/edit':
        track=next((t for t in data['tracks'] if t['id']==payload.get('track_id')),None)
        selected=next((f for f in (track or {}).get('files',[]) if f['id']==payload.get('file_id') and f['exists']),None)
        if not track or not selected:
            return handler._json({'error':'请先选择当前清单中的完整成品'},404)
        if payload.get('file_signature') != selected['signature']:
            return handler._json({'error':'视频已变化，请刷新成品清单后再进入编辑'},409)
        from review_workflow import editor_workspace
        return handler._json(editor_workspace(track,selected,ROOT,NATIVE_ROOT))
    if route == '/api/review/reviews':
        track = next((t for t in data['tracks'] if t['id'] == payload.get('track_id')), None)
        if not track:
            return handler._json({'error':'曲目不在成品清单中'}, 404)
        try:
            record = REVIEWS.save(track, payload)
        except ReviewConflict as exc:
            return handler._json({'error':str(exc), 'current':exc.current}, 409)
        return handler._json({'ok':True, 'review':record, 'path':str(REVIEWS.path)})
    identifier = payload.get('id')
    path = allowed.get(identifier) if isinstance(identifier, str) else None
    if not path or not path.is_file() or not any(path.resolve().is_relative_to(root.resolve()) for root in (ROOT,NATIVE_ROOT)):
        return handler._json({'error':'文件不在当前成品清单中'}, 404)
    if not REVEAL_LOCK.acquire(blocking=False):
        return handler._json({'error':'正在打开资源管理器，请稍候'}, 429)
    try:
        reveal_in_explorer(path)
    finally:
        REVEAL_LOCK.release()
    return handler._json({'ok':True, 'path':str(path)})
