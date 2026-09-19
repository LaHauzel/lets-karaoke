"""HTTP contract tests for shared rule defaults, validation and SOFA routing."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import webui


class HttpPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_root = webui.OUT_ROOT
        webui.OUT_ROOT = Path(cls.temp.name)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), webui.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        webui.OUT_ROOT = cls.old_root
        cls.temp.cleanup()

    def test_defaults_and_schema(self):
        meta = requests.get(self.base + '/api/meta', timeout=5).json()
        self.assertEqual(set(meta['rule_schema']), set(meta['rule_profiles']['automatic']))
        self.assertFalse(meta['rule_profiles']['automatic']['relocate_lowconf'])
        self.assertTrue(meta['rule_profiles']['legacy']['relocate_lowconf'])

    def test_invalid_rules_are_http_400(self):
        for rules in ('[]', '{"vocal_thr":-1}', '{"tail_extend":"false"}'):
            response = requests.post(self.base + '/api/run', timeout=5,
                files={'media': ('test.wav', b'test')},
                data={'whisper': '1', 'lyrics_text': 'a', 'rules': rules})
            self.assertEqual(response.status_code, 400, response.text)

    def test_both_routes_receive_rules(self):
        for route in ('whisper', 'sofa', 'asr'):
            done = threading.Event()
            configs = []
            def worker(job, cfg):
                configs.append(cfg)
                done.set()
            with patch.object(webui, '_run_job', side_effect=worker):
                response = requests.post(self.base + '/api/run', timeout=5,
                    files={'media': ('test.wav', b'test')},
                    data={route: '1', 'lyrics_text': 'a', 'rules': '{"boundary_limit":0}'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(done.wait(5))
                self.assertEqual(configs[0]['alignment_profile'], 'balanced')
                self.assertEqual(configs[0]['rules']['boundary_limit'], 0)
                if route == 'asr':
                    self.assertFalse(configs[0].get('lyrics_text'))


if __name__ == '__main__':
    unittest.main()
