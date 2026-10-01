"""Remote-bind refusal, error hygiene and capability reporting; no GPU or models."""
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


class ExposureTest(unittest.TestCase):
    def test_remote_bind_requires_explicit_opt_in(self):
        for host in ('0.0.0.0', '192.168.1.20', '::'):
            with self.subTest(host=host):
                self.assertEqual(webui.main(['--host', host, '--no-open', '--no-warmup']), 2)

    def test_capabilities_report_reasons_for_missing_features(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(webui.model_paths, 'WHISPER_DIR', Path(temp) / 'whisper'), \
                patch.object(webui.model_paths, 'QWEN_ASR_DIR', Path(temp) / 'asr'), \
                patch.object(webui.model_paths, 'QWEN_ALIGNER_DIR', Path(temp) / 'aligner'), \
                patch.object(webui, 'SOFA_CKPT', Path(temp) / 'missing.ckpt'):
            (Path(temp) / 'whisper').mkdir()
            (Path(temp) / 'whisper' / 'base.pt').write_bytes(b'')
            caps = webui.capabilities()
        self.assertEqual(caps['whisper_models'], ['base'])
        for key in ('sofa', 'asr', 'qwen'):
            self.assertFalse(caps[key]['ok'])
            self.assertTrue(caps[key]['reason'])


class ErrorHygieneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), webui.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_meta_exposes_capabilities(self):
        meta = requests.get(self.base + '/api/meta', timeout=5).json()
        self.assertIn('whisper', meta['capabilities'])

    def test_unexpected_post_errors_do_not_return_tracebacks(self):
        with patch.object(webui.Handler, '_api_rerender', side_effect=RuntimeError('boom')), \
                patch.object(webui.traceback, 'print_exc'):
            r = requests.post(self.base + '/api/rerender', json={'job': 'x'}, timeout=5)
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.json()['error'], 'RuntimeError: boom')
        self.assertNotIn('Traceback', r.text)


if __name__ == '__main__':
    unittest.main()
