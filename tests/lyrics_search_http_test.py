"""Local HTTP boundaries for lyrics lookup; all provider requests are mocked."""
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import webui
from lyrics_search import LyricsSearchError


class LyricsSearchHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), webui.Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)

    def test_search_and_detail_dispatch_keep_the_lookup_contract(self):
        with patch('lyrics_search.search', return_value={'provider': 'LRCLIB', 'results': []}) as search:
            response = requests.post(self.base + '/api/lyrics/search',
                json={'track_name': '测试曲', 'artist_name': '测试艺人'}, timeout=5)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['results'], [])
            search.assert_called_once_with('测试曲', '测试艺人')
        with patch('lyrics_search.get_lyrics', return_value={'id': 123, 'plain_lyrics': 'synthetic words'}) as get:
            response = requests.post(self.base + '/api/lyrics/get', json={'id': 123}, timeout=5)
            self.assertEqual(response.status_code, 200)
            get.assert_called_once_with(123)

    def test_unknown_fields_cross_origin_and_wrong_content_type_never_call_provider(self):
        with patch('lyrics_search.search') as search, patch('lyrics_search.get_lyrics') as get:
            cases = [('/api/lyrics/search', {'track_name': 'test', 'url': 'https://example.org'}, {}, 400),
                     ('/api/lyrics/search', {'track_name': 'test', 'media': 'local-file'}, {}, 400),
                     ('/api/lyrics/get', {'id': 123, 'lyrics': 'private text'}, {}, 400),
                     ('/api/lyrics/search', {'track_name': 'test'}, {'Origin': 'https://example.org'}, 403),
                     ('/api/lyrics/get', {'id': 123}, {'Content-Type': 'text/plain'}, 415)]
            with requests.Session() as session:
                for path, body, headers, code in cases:
                    with self.subTest(path=path, code=code):
                        response = session.post(self.base + path, json=body, headers=headers, timeout=5)
                        self.assertEqual(response.status_code, code, response.text)
                self.assertEqual(session.get(self.base + '/api/meta', timeout=5).status_code, 200)
            search.assert_not_called()
            get.assert_not_called()

    def test_provider_errors_return_friendly_status_and_cooldown(self):
        for code, retry in ((429, 30), (503, 1), (504, None), (404, None)):
            with self.subTest(code=code), patch('lyrics_search.search',
                    side_effect=LyricsSearchError('请稍后再试', status=code, retry_after=retry)):
                response = requests.post(self.base + '/api/lyrics/search', json={'track_name': 'test'}, timeout=5)
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json()['error'], '请稍后再试')
                self.assertEqual(response.json().get('retry_after'), retry)

    def test_input_validation_and_body_limit_prevent_network_requests(self):
        with patch('lyrics_search._read_json') as network:
            for body in ([], {'track_name': ''}, {'track_name': 'https://127.0.0.1'}, {'id': '../../private'}, {'id': True}):
                path = '/api/lyrics/get' if isinstance(body, dict) and 'id' in body else '/api/lyrics/search'
                response = requests.post(self.base + path, json=body, timeout=5)
                self.assertEqual(response.status_code, 400, response.text)
            # A large declared length is rejected before reading or allocation.
            response = requests.post(self.base + '/api/lyrics/search', data=b'',
                headers={'Content-Type': 'application/json', 'Content-Length': '4097'}, timeout=5)
            self.assertEqual(response.status_code, 413)
            response = requests.get(self.base + '/api/lyrics/search?track_name=test', timeout=5)
            self.assertEqual(response.status_code, 404)
            network.assert_not_called()

    def test_metadata_and_new_assets_are_available_without_network(self):
        with patch('lyrics_search._read_json') as network:
            meta = requests.get(self.base + '/api/meta', timeout=5).json()
            self.assertTrue(meta['features']['lyrics_search'])
            self.assertFalse(meta['lyrics_search']['requires_api_key'])
            for name, kind in (('theme.css', 'text/css'), ('karaoke.css', 'text/css'),
                               ('lyrics_search.css', 'text/css'), ('lyrics_search.js', 'text/javascript')):
                response = requests.get(self.base + '/' + name, timeout=5)
                self.assertEqual(response.status_code, 200, name)
                self.assertTrue(response.headers['Content-Type'].startswith(kind))
            network.assert_not_called()

    def test_deep_or_malformed_json_returns_400_without_provider_request(self):
        bodies = [b'{', b'\xff',
                  b'{"track_name":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}']
        with patch('lyrics_search._read_json') as network:
            with requests.Session() as session:
                for body in bodies:
                    with self.subTest(length=len(body)):
                        response = session.post(self.base + '/api/lyrics/search', data=body,
                            headers={'Content-Type': 'application/json'}, timeout=5)
                        self.assertEqual(response.status_code, 400, response.text)
                        self.assertTrue(response.json()['error'])
                self.assertEqual(session.get(self.base + '/api/meta', timeout=5).status_code, 200)
            network.assert_not_called()

    def test_early_eof_rejects_even_a_complete_json_body(self):
        body = b'{"track_name":"test"}'
        with patch('lyrics_search.search') as search:
            with socket.create_connection(self.server.server_address, timeout=5) as connection:
                connection.sendall((f'POST /api/lyrics/search HTTP/1.1\r\n'
                    f'Host: 127.0.0.1:{self.server.server_port}\r\n'
                    f'Content-Type: application/json\r\nContent-Length: {len(body) + 8}\r\n\r\n').encode()
                    + body)
                connection.shutdown(socket.SHUT_WR)
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 400)
                self.assertEqual(response.getheader('Connection'), 'close')
                self.assertIn('不完整', json.loads(response.read())['error'])
            search.assert_not_called()

    def test_trickle_body_hits_total_deadline_despite_continuous_data(self):
        finished = threading.Event()
        with patch('lyrics_search.search') as search:
            with socket.create_connection(self.server.server_address, timeout=8) as connection:
                started = time.monotonic()
                connection.sendall((f'POST /api/lyrics/search HTTP/1.1\r\n'
                    f'Host: 127.0.0.1:{self.server.server_port}\r\n'
                    f'Content-Type: application/json\r\nContent-Length: 256\r\n\r\n ').encode())
                def trickle():
                    while not finished.wait(.2):
                        try:
                            connection.sendall(b' ')
                        except OSError:
                            return
                sender = threading.Thread(target=trickle, daemon=True)
                sender.start()
                try:
                    response = http.client.HTTPResponse(connection)
                    response.begin()
                    self.assertEqual(response.status, 408)
                    self.assertEqual(response.getheader('Connection'), 'close')
                    self.assertIn('超时', json.loads(response.read())['error'])
                    self.assertLess(time.monotonic() - started, 8)
                finally:
                    finished.set()
                    sender.join(2)
            search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
