"""Offline LRCLIB transport, validation, cache and request-policy regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import http.client
import io
import json
from pathlib import Path
import socket
import ssl
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import lyrics_search as lyrics


RECORD = {'id': 42, 'trackName': 'Synthetic song', 'artistName': 'Test artist',
          'albumName': 'Test album', 'duration': 120.5, 'instrumental': False,
          'plainLyrics': 'Synthetic line', 'syncedLyrics': '[00:01.00]Synthetic line'}


class Response:
    def __init__(self, body, url=None, status=200, headers=None):
        self.body = io.BytesIO(body)
        self.url, self.status = url, status
        self.headers = {'Content-Type': 'application/json', **(headers or {})}
        self.closed = False

    def geturl(self):
        return self.url

    def read1(self, count):
        return self.body.read(count)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class LyricsSearchTests(unittest.TestCase):
    def setUp(self):
        with lyrics._STATE_LOCK:
            lyrics._CACHE.clear()
            lyrics._CACHE_BYTES = 0
            lyrics._RETRY_UNTIL = 0
        lyrics._LAST_FINISHED = -float('inf')
        self.limits = patch.dict(lyrics.LIMITS, {'request_interval_seconds': 0})
        self.limits.start()
        self.addCleanup(self.limits.stop)

    def transport(self, payload=None, body=None, headers=None, status=200):
        opener = Mock()
        def open_response(request, timeout):
            return Response(body if body is not None else json.dumps(payload).encode('utf-8'),
                            request.full_url, status, headers)
        opener.open.side_effect = open_response
        return patch.object(lyrics.urllib.request, 'build_opener', return_value=opener), opener

    def failure(self, function, status):
        with self.assertRaises(lyrics.LyricsSearchError) as caught:
            function()
        self.assertEqual(caught.exception.status, status)
        return caught.exception

    def test_search_maps_metadata_and_does_not_expose_lyrics(self):
        transport, opener = self.transport([RECORD])
        with transport:
            result = lyrics.search(' Synthetic song ', ' Test artist ')
        self.assertEqual(result['provider'], 'LRCLIB')
        self.assertEqual(result['results'], [{'id': 42, 'title': 'Synthetic song', 'artist': 'Test artist',
            'album': 'Test album', 'duration': 120.5, 'instrumental': False,
            'has_plain': True, 'has_synced': True}])
        request = opener.open.call_args.args[0]
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query),
                         {'track_name': ['Synthetic song'], 'artist_name': ['Test artist']})
        self.assertNotIn('Synthetic line', json.dumps(result))
        self.assertEqual(result['source_url'], request.full_url)

    def test_get_maps_plain_and_synced_lyrics_with_canonical_id(self):
        transport, opener = self.transport(RECORD)
        with transport:
            result = lyrics.get_lyrics('00042')
            again = lyrics.get_lyrics(42)
        self.assertEqual(result, again)
        self.assertEqual(result['source_url'], 'https://lrclib.net/api/get/42')
        self.assertEqual(result['plain_lyrics'], RECORD['plainLyrics'])
        self.assertEqual(result['synced_lyrics'], RECORD['syncedLyrics'])
        opener.open.assert_called_once()

    def test_names_and_ids_reject_urls_controls_wrong_types_and_oversized_values_before_network(self):
        with patch.object(lyrics, '_read_json') as network:
            for title in ('', '   ', 'https://example.com', '//127.0.0.1', 'file:///secret',
                          'www.example.com', 'mailto:user@example.com', 'song\nother', 'song\x00', 'a' * 201,
                          'has https://example.com suffix', None, ['song']):
                with self.subTest(title=repr(title)):
                    self.failure(lambda: lyrics.search(title), 400)
            for artist in ('https://example.com', 'name\tother', None, 1):
                with self.subTest(artist=repr(artist)):
                    self.failure(lambda: lyrics.search('song', artist), 400)
            for identifier in (None, 0, -1, True, 1.5, '1.5', 'https://example.com',
                               '../42', '4/2', '+42', '４２', 2**63, {}):
                with self.subTest(identifier=repr(identifier)):
                    self.failure(lambda: lyrics.get_lyrics(identifier), 400)
            network.assert_not_called()

    def test_unicode_and_query_characters_are_encoded_into_fixed_host(self):
        transport, opener = self.transport([])
        with transport:
            result = lyrics.search('你好 + A&B? x=y # Test', 'AC/DC')
        url = opener.open.call_args.args[0].full_url
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path, parsed.fragment),
                         ('https', 'lrclib.net', '/api/search', ''))
        self.assertEqual(urllib.parse.parse_qs(parsed.query)['track_name'], ['你好 + A&B? x=y # Test'])
        self.assertEqual(result['results'], [])

    def test_transport_has_verified_tls_no_proxy_no_redirect_and_no_body(self):
        transport, opener = self.transport([])
        with transport as build:
            lyrics.search('song')
        handlers = build.call_args.args
        proxy = next(handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler))
        redirect = next(handler for handler in handlers if isinstance(handler, lyrics._NoRedirect))
        https = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPSHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirect.redirect_request(None, None, 302, '', {}, 'http://127.0.0.1'))
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')
        self.assertIn('lets-karaoke', request.get_header('User-agent'))
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)

    def test_redirect_and_error_statuses_do_not_read_provider_body(self):
        for code, expected in ((301, 502), (302, 502), (307, 502), (308, 502), (404, 404), (500, 502)):
            with self.subTest(code=code):
                body = Mock()
                failure = urllib.error.HTTPError('https://lrclib.net/api/search', code, 'error', {}, body)
                opener = Mock(); opener.open.side_effect = failure
                with patch.object(lyrics.urllib.request, 'build_opener', return_value=opener):
                    self.failure(lambda: lyrics.search('song'), expected)
                body.read.assert_not_called()
                body.close.assert_called_once()

    def test_unexpected_response_url_is_rejected(self):
        opener = Mock()
        response = Response(b'[]', 'https://other.example/api/search')
        opener.open.return_value = response
        with patch.object(lyrics.urllib.request, 'build_opener', return_value=opener):
            self.failure(lambda: lyrics.search('song'), 502)
        self.assertEqual(response.body.tell(), 0)
        self.assertTrue(response.closed)

    def test_429_honors_retry_after_without_retry_or_blocking(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://lrclib.net/api/search', 429,
            'rate limited', {'Retry-After': '120'}, io.BytesIO(b'private provider body'))
        with patch.object(lyrics.urllib.request, 'build_opener', return_value=opener):
            first = self.failure(lambda: lyrics.search('song'), 429)
            second = self.failure(lambda: lyrics.search('different song'), 429)
        self.assertEqual(first.retry_after, 120)
        self.assertGreaterEqual(second.retry_after, 119)
        opener.open.assert_called_once()
        self.assertNotIn('private provider body', str(first))

    def test_retry_after_supports_dates_and_invalid_header_default(self):
        header = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=70), usegmt=True)
        self.assertGreaterEqual(lyrics._retry_seconds(header), 69)
        self.assertEqual(lyrics._retry_seconds('malformed'), 30)
        self.assertEqual(lyrics._retry_seconds(None), 30)
        self.assertEqual(lyrics._retry_seconds('0'), 1)

    def test_bad_content_type_compression_json_and_shape_are_rejected(self):
        cases = [({'Content-Type': 'text/html'}, b'<html>not JSON</html>'),
                 ({'Content-Encoding': 'gzip'}, b'[]'), ({}, b'invalid private contents'),
                 ({}, b'{"secret": "private"}'), ({}, b'[NaN]'), ({}, b'[42]')]
        for headers, body in cases:
            with self.subTest(headers=headers, body=body):
                transport, _ = self.transport(body=body, headers=headers)
                with transport:
                    error = self.failure(lambda: lyrics.search('song'), 502)
                self.assertNotIn('private', str(error))

    def test_oversized_response_is_rejected_with_and_without_content_length(self):
        with patch.dict(lyrics.LIMITS, {'max_response_bytes': 10}):
            for headers in ({'Content-Length': '11'}, {}, {'Content-Length': '-1'}):
                with self.subTest(headers=headers):
                    transport, _ = self.transport(body=b' ' * 11, headers=headers)
                    with transport:
                        self.failure(lambda: lyrics.search('song'), 502)

    def test_response_total_deadline_stops_slow_chunks(self):
        response = Response(b'[]', 'https://lrclib.net/api/search?track_name=song')
        opener = Mock(); opener.open.return_value = response
        with (patch.object(lyrics.urllib.request, 'build_opener', return_value=opener),
              patch.object(lyrics.time, 'monotonic', side_effect=[0, 16])):
            self.failure(lambda: lyrics._read_json(response.url), 504)
        self.assertTrue(response.closed)

    def test_content_length_must_match_even_when_short_response_is_valid_json(self):
        for declared in ('1', '3'):
            with self.subTest(declared=declared):
                transport, _ = self.transport(body=b'[]', headers={'Content-Length': declared})
                with transport:
                    self.failure(lambda: lyrics.search('song'), 502)
        transport, _ = self.transport(body=b'[]', headers={'Content-Length': '2'})
        with transport:
            self.assertEqual(lyrics.search('song')['results'], [])

    def test_network_timeout_and_tls_errors_are_safe(self):
        for original, status in ((socket.timeout('private'), 504),
                (urllib.error.URLError(socket.timeout('private')), 504),
                (urllib.error.URLError(ssl.SSLError('private')), 502),
                (urllib.error.URLError('private'), 502), (OSError('private'), 502),
                (http.client.IncompleteRead(b'private'), 502)):
            with self.subTest(original=type(original)):
                opener = Mock(); opener.open.side_effect = original
                with patch.object(lyrics.urllib.request, 'build_opener', return_value=opener):
                    failure = self.failure(lambda: lyrics.search('song'), status)
                self.assertNotIn('private', str(failure))

    def test_invalid_detail_types_and_mismatched_id_are_rejected(self):
        for change in ({'id': 43}, {'id': False}, {'duration': float('inf')},
                       {'duration': -1}, {'duration': 10**400}, {'plainLyrics': ['unexpected']},
                       {'trackName': 3}, {'plainLyrics': '\ud800'}):
            with self.subTest(change=change):
                transport, _ = self.transport({**RECORD, **change})
                with transport:
                    self.failure(lambda: lyrics.get_lyrics(42), 502)

    def test_instrumental_record_with_null_lyrics_is_supported(self):
        transport, _ = self.transport({**RECORD, 'plainLyrics': None, 'syncedLyrics': None,
                                      'instrumental': True})
        with transport:
            result = lyrics.get_lyrics(42)
        self.assertTrue(result['instrumental'])
        self.assertFalse(result['has_plain'])
        self.assertFalse(result['has_synced'])
        self.assertEqual(result['plain_lyrics'], '')
        self.assertEqual(result['synced_lyrics'], '')

    def test_cache_uses_independent_copies_and_expires(self):
        transport, opener = self.transport([RECORD])
        clock = [0.]
        with transport, patch.object(lyrics.time, 'monotonic', side_effect=lambda: clock[0]):
            first = lyrics.search('song')
            first['results'][0]['title'] = 'tampered'
            self.assertEqual(lyrics.search('song')['results'][0]['title'], 'Synthetic song')
            clock[0] = lyrics.LIMITS['cache_ttl_seconds'] + 1
            lyrics.search('song')
        self.assertEqual(opener.open.call_count, 2)

    def test_cache_is_bounded_by_entry_count_and_bytes(self):
        transport, opener = self.transport([])
        with transport, patch.dict(lyrics.LIMITS, {'cache_max_entries': 2}):
            for title in ('first', 'second', 'third'):
                lyrics.search(title)
            self.assertEqual(len(lyrics._CACHE), 2)
            self.assertNotIn(('search', 'first', ''), lyrics._CACHE)
            self.assertLessEqual(lyrics._CACHE_BYTES, lyrics.LIMITS['cache_max_bytes'])
        with patch.dict(lyrics.LIMITS, {'cache_max_bytes': 200}):
            lyrics._cache_put(('synthetic', 1), {'data': 'x' * 180})
            lyrics._cache_put(('synthetic', 2), {'data': 'y' * 180})
            self.assertLessEqual(lyrics._CACHE_BYTES, 200)
            self.assertEqual(len(lyrics._CACHE), 1)
            lyrics._cache_put(('too-big',), {'data': 'z' * 201})
            self.assertNotIn(('too-big',), lyrics._CACHE)

    def test_errors_are_not_cached_and_empty_search_is_cached(self):
        with patch.object(lyrics, '_read_json', side_effect=[
                lyrics.LyricsSearchError('暂时失败'), [], []]) as network:
            self.failure(lambda: lyrics.search('song'), 502)
            self.assertEqual(lyrics.search('song')['results'], [])
            self.assertEqual(lyrics.search('song')['results'], [])
        self.assertEqual(network.call_count, 2)

    def test_search_caps_results_at_provider_limit(self):
        transport, _ = self.transport([dict(RECORD, id=index + 1) for index in range(25)])
        with transport:
            result = lyrics.search('song')
        self.assertEqual(len(result['results']), 20)

    def test_sequential_requests_include_minimum_gap(self):
        transport, opener = self.transport([])
        with (transport, patch.dict(lyrics.LIMITS, {'request_interval_seconds': .3}),
              patch.object(lyrics.time, 'monotonic', return_value=10),
              patch.object(lyrics.time, 'sleep') as sleep):
            lyrics.search('first')
            lyrics.search('second')
        sleep.assert_called_once_with(.3)
        self.assertEqual(opener.open.call_count, 2)

    def test_concurrent_same_query_only_sends_one_request(self):
        started, finish = threading.Event(), threading.Event()
        def fetch(url):
            started.set()
            self.assertTrue(finish.wait(2))
            return [RECORD]
        with patch.object(lyrics, '_read_json', side_effect=fetch) as network:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(lyrics.search, 'song')
                self.assertTrue(started.wait(2))
                second = pool.submit(lyrics.search, 'song')
                finish.set()
                self.assertEqual(first.result(timeout=2), second.result(timeout=2))
        network.assert_called_once()

    def test_busy_request_is_bounded_and_cached_result_survives_cooldown(self):
        with patch.object(lyrics, '_REQUEST_LOCK') as request_lock:
            request_lock.acquire.return_value = False
            self.failure(lambda: lyrics.search('song'), 503)
            request_lock.acquire.assert_called_once_with(timeout=1.)
        transport, opener = self.transport([])
        with transport:
            lyrics.search('song')
            lyrics._RETRY_UNTIL = lyrics.time.monotonic() + 100
            self.assertEqual(lyrics.search('song')['results'], [])
        opener.open.assert_called_once()

    def test_metadata_is_copy_and_requires_no_key(self):
        meta = lyrics.metadata()
        self.assertFalse(meta['requires_api_key'])
        self.assertEqual(meta['documentation_url'], 'https://lrclib.net/docs')
        meta['limits']['max_name_chars'] = 1
        self.assertEqual(lyrics.LIMITS['max_name_chars'], 200)


if __name__ == '__main__':
    unittest.main()
