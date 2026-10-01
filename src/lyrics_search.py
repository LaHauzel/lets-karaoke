"""Read-only LRCLIB lookup; local media and lyrics never enter these requests."""
from collections import OrderedDict
import copy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import re
import socket
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request


PROVIDER = 'LRCLIB'
SOURCE_URL = 'https://lrclib.net'
DOCUMENTATION_URL = SOURCE_URL + '/docs'
LIMITS = {
    'max_name_chars': 200,
    'max_results': 20,
    'max_response_bytes': 1024 * 1024,
    'request_timeout_seconds': 8,
    'response_deadline_seconds': 15,
    'request_interval_seconds': 0.3,
    'cache_ttl_seconds': 180,
    'cache_max_entries': 64,
    'cache_max_bytes': 4 * 1024 * 1024,
}
_USER_AGENT = 'lets-karaoke/1.0 (https://github.com/LaHauzel/lets-karaoke)'
_URL_LIKE = re.compile(r'(?:[a-z][a-z0-9+.-]*://|(?:https?|ftp|file|data|javascript|mailto|tel|urn|about):|www\.)', re.I)
_CACHE = OrderedDict()
_CACHE_BYTES = 0
_STATE_LOCK = threading.Lock()
_REQUEST_LOCK = threading.Lock()
_LAST_FINISHED = -math.inf
_RETRY_UNTIL = 0.0


class LyricsSearchError(Exception):
    def __init__(self, message, status=502, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def metadata():
    return {'provider': PROVIDER, 'source_url': SOURCE_URL,
            'documentation_url': DOCUMENTATION_URL, 'requires_api_key': False,
            'limits': copy.deepcopy(LIMITS)}


def _name(value, label, required=False):
    if not isinstance(value, str):
        raise LyricsSearchError(f'{label}必须是文字', status=400)
    if any(unicodedata.category(char).startswith('C') for char in value):
        raise LyricsSearchError(f'{label}不能含控制字符', status=400)
    value = value.strip()
    if required and not value:
        raise LyricsSearchError('请填写歌名', status=400)
    if len(value) > LIMITS['max_name_chars']:
        raise LyricsSearchError(f'{label}过长，请缩短后再试', status=400)
    if _URL_LIKE.search(value) or value.startswith(('//', '\\\\')):
        raise LyricsSearchError('仅支持歌名和艺人名，不支持网址', status=400)
    return value


def _record_id(value):
    if isinstance(value, bool):
        raise LyricsSearchError('歌词编号必须是正整数', status=400)
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,19}', value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or not 0 < value <= 9223372036854775807:
        raise LyricsSearchError('歌词编号必须是正整数', status=400)
    return value


def _retry_seconds(header):
    if header:
        header = header.strip()
        if re.fullmatch(r'[0-9]{1,10}', header):
            return max(1, int(header))
        try:
            stamp = parsedate_to_datetime(header)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return max(1, math.ceil((stamp - datetime.now(timezone.utc)).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            pass
    return 30


def _http_failure(status, headers):
    global _RETRY_UNTIL
    if status == 429:
        seconds = _retry_seconds((headers or {}).get('Retry-After'))
        with _STATE_LOCK:
            _RETRY_UNTIL = max(_RETRY_UNTIL, time.monotonic() + seconds)
        return LyricsSearchError('歌词服务请求过于频繁，请稍后再试', status=429,
                                 retry_after=seconds)
    if status == 404:
        return LyricsSearchError('没有找到这条歌词，可能已被删除', status=404)
    if 300 <= status < 400:
        return LyricsSearchError('歌词服务返回了不支持的跳转', status=502)
    return LyricsSearchError('歌词服务暂时不可用，请稍后再试', status=502)


def _read_json(url):
    """No redirects, ambient proxies, cookies, or relaxed TLS verification."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    request = urllib.request.Request(url, method='GET', headers={
        'User-Agent': _USER_AGENT, 'Accept': 'application/json',
        'Accept-Encoding': 'identity',
    })
    deadline = time.monotonic() + LIMITS['response_deadline_seconds']
    try:
        with opener.open(request, timeout=LIMITS['request_timeout_seconds']) as response:
            if response.geturl() != url:
                raise LyricsSearchError('歌词服务返回了不支持的跳转', status=502)
            if response.status != 200:
                raise _http_failure(response.status, response.headers)
            kind = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if kind != 'application/json':
                raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502)
            encoding = response.headers.get('Content-Encoding', 'identity').strip().lower()
            if encoding not in ('', 'identity'):
                raise LyricsSearchError('歌词服务返回了不支持的压缩格式', status=502)
            maximum = LIMITS['max_response_bytes']
            length = response.headers.get('Content-Length')
            if length is not None:
                if not re.fullmatch(r'[0-9]+', length) or len(length) > 12:
                    raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502)
                if int(length) > maximum:
                    raise LyricsSearchError('歌词服务响应过大，请换个歌名再试', status=502)
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise LyricsSearchError('歌词服务响应超时，请稍后再试', status=504)
                # read1 returns available data instead of waiting to fill the
                # entire limit; this permits a total deadline for trickle data.
                chunk = response.read1(min(65536, maximum + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise LyricsSearchError('歌词服务响应过大，请换个歌名再试', status=502)
                chunks.append(chunk)
            if time.monotonic() >= deadline:
                raise LyricsSearchError('歌词服务响应超时，请稍后再试', status=504)
            if length is not None and size != int(length):
                raise LyricsSearchError('歌词服务响应不完整，请稍后再试', status=502)
            def invalid_constant(value):
                raise ValueError('Non-finite JSON constant')
            return json.loads(b''.join(chunks).decode('utf-8'), parse_constant=invalid_constant)
    except urllib.error.HTTPError as error:
        failure = _http_failure(error.code, error.headers)
        error.close()
        raise failure from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise LyricsSearchError('歌词服务连接超时，请稍后再试', status=504) from None
        raise LyricsSearchError('无法连接歌词服务，请检查网络后再试', status=502) from None
    except (socket.timeout, TimeoutError):
        raise LyricsSearchError('歌词服务响应超时，请稍后再试', status=504) from None
    except (UnicodeError, ValueError, RecursionError):
        raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502) from None
    except http.client.HTTPException:
        raise LyricsSearchError('歌词服务连接中断，请稍后再试', status=502) from None
    except (OSError, ssl.SSLError):
        raise LyricsSearchError('无法连接歌词服务，请检查网络后再试', status=502) from None


def _text(record, key, default=''):
    value = record.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502)
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502) from None
    return value


def _summary(record):
    if not isinstance(record, dict):
        raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502)
    try:
        identifier = _record_id(record.get('id'))
    except LyricsSearchError:
        raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502) from None
    duration = record.get('duration')
    if duration is not None:
        try:
            valid_duration = (not isinstance(duration, bool) and isinstance(duration, (int, float))
                              and math.isfinite(duration) and duration >= 0)
        except OverflowError:
            valid_duration = False
        if not valid_duration:
            raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502)
    return {'id': identifier, 'title': _text(record, 'trackName', _text(record, 'name')),
            'artist': _text(record, 'artistName'), 'album': _text(record, 'albumName'),
            'duration': duration, 'instrumental': record.get('instrumental') is True,
            'has_plain': bool(_text(record, 'plainLyrics').strip()),
            'has_synced': bool(_text(record, 'syncedLyrics').strip())}


def _cache_get(key):
    global _CACHE_BYTES
    now = time.monotonic()
    with _STATE_LOCK:
        for stale in [item for item, entry in _CACHE.items() if entry[0] <= now]:
            _CACHE_BYTES -= _CACHE.pop(stale)[2]
        entry = _CACHE.get(key)
        if entry is None:
            return None
        _CACHE.move_to_end(key)
        return copy.deepcopy(entry[1])


def _cache_put(key, data):
    global _CACHE_BYTES
    size = len(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    if size > LIMITS['cache_max_bytes']:
        return
    with _STATE_LOCK:
        old = _CACHE.pop(key, None)
        if old:
            _CACHE_BYTES -= old[2]
        while _CACHE and (len(_CACHE) >= LIMITS['cache_max_entries']
                          or _CACHE_BYTES + size > LIMITS['cache_max_bytes']):
            _CACHE_BYTES -= _CACHE.popitem(last=False)[1][2]
        _CACHE[key] = (time.monotonic() + LIMITS['cache_ttl_seconds'], copy.deepcopy(data), size)
        _CACHE_BYTES += size


def _lookup(key, url, normalize):
    global _LAST_FINISHED
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if not _REQUEST_LOCK.acquire(timeout=1.0):
        raise LyricsSearchError('歌词服务正忙，请稍后再试', status=503, retry_after=1)
    try:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        now = time.monotonic()
        with _STATE_LOCK:
            remaining = _RETRY_UNTIL - now
        if remaining > 0:
            raise LyricsSearchError('歌词服务请求过于频繁，请稍后再试', status=429,
                                     retry_after=math.ceil(remaining))
        delay = LIMITS['request_interval_seconds'] - (now - _LAST_FINISHED)
        if delay > 0:
            time.sleep(delay)
        try:
            result = normalize(_read_json(url))
        finally:
            _LAST_FINISHED = time.monotonic()
        _cache_put(key, result)
        return result
    finally:
        _REQUEST_LOCK.release()


def search(track_name, artist_name=''):
    title, artist = _name(track_name, '歌名', required=True), _name(artist_name, '艺人名')
    query = {'track_name': title}
    if artist:
        query['artist_name'] = artist
    url = SOURCE_URL + '/api/search?' + urllib.parse.urlencode(query)
    def normalize(records):
        if not isinstance(records, list):
            raise LyricsSearchError('歌词服务返回格式异常，请稍后再试', status=502)
        return {'provider': PROVIDER, 'source_url': url,
                'results': [_summary(record) for record in records[:LIMITS['max_results']]]}
    return _lookup(('search', title, artist), url, normalize)


def get_lyrics(record_id):
    identifier = _record_id(record_id)
    url = SOURCE_URL + '/api/get/' + str(identifier)
    def normalize(record):
        result = _summary(record)
        if result['id'] != identifier:
            raise LyricsSearchError('歌词服务返回了不匹配的编号', status=502)
        return {'provider': PROVIDER, 'source_url': url, **result,
                'plain_lyrics': _text(record, 'plainLyrics'),
                'synced_lyrics': _text(record, 'syncedLyrics')}
    return _lookup(('get', identifier), url, normalize)
