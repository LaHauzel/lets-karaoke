"""SOFA crop coordinates and Japanese-only routing, without GPU inference."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import requests
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import sofa_backend
import webui
from pipeline import parse_lyrics


class PhoneTier(list):
    name = 'phones'


class SofaCropTests(unittest.TestCase):
    def test_textgrid_clock_uses_actual_cropped_sample_origin(self):
        # The first case catches the old fixed 200 ms error. The other cases
        # catch using a constant pad when that pad was truncated at audio zero.
        for start, end in [(5.123, 6.123), (.1, 1.1), (0, 1)]:
            with self.subTest(start=start), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                directory = root / 'job'
                directory.mkdir()
                vocals = root / 'vocals.wav'
                sr = 1000
                sf.write(vocals, np.zeros(10 * sr), sr)
                clip_sample = int(max(0, start - .2) * sr)
                clip_start = clip_sample / sr
                relative_start = start - clip_start
                tiers = PhoneTier([
                    SimpleNamespace(minTime=relative_start,
                                    maxTime=relative_start + .4, mark='a'),
                    SimpleNamespace(minTime=relative_start + .4,
                                    maxTime=relative_start + 1, mark='i'),
                ])
                fake_grid = SimpleNamespace(TextGrid=SimpleNamespace(
                    fromFile=lambda path: SimpleNamespace(tiers=[tiers])))
                job = webui.Job(id='sofa-test', dir=directory)
                config = {'lang': 'auto', 'lyrics_text': '[00:05.000]あい',
                          'rules': {'tx_crosscheck': False}}

                def whisper_windows(job, cfg, cancel):
                    self.assertEqual(cfg['lang'], 'ja')
                    from asr_lyrics import AsrLine, to_srt
                    (directory / 'whisper_lyrics.srt').write_text(
                        to_srt([AsrLine('あい', start, end, [], .9)]), encoding='utf-8')

                def infer(cmd, **kwargs):
                    folder = Path(cmd[cmd.index('--folder') + 1])
                    audio, sample_rate = sf.read(folder / 'line000.wav')
                    self.assertEqual(sample_rate, sr)
                    self.assertEqual(len(audio), int((end + .2) * sr) - clip_sample)
                    textgrids = folder / 'TextGrid'
                    textgrids.mkdir()
                    (textgrids / 'line000.TextGrid').write_text('model-result', encoding='utf-8')
                    return subprocess.CompletedProcess(cmd, 0, b'', b'')

                with patch.object(webui, 'ROOT', root), \
                        patch.object(webui, '_audio44', return_value=vocals), \
                        patch.object(webui, '_vocals_stem', return_value=(vocals, None)), \
                        patch.object(webui, '_whisper_prepass', side_effect=whisper_windows), \
                        patch('process_runner.run_process', side_effect=infer), \
                        patch.dict(sys.modules, {'textgrid': fake_grid}), \
                        patch('whisper_align.vocal_intervals', return_value=[]), \
                        patch.object(webui, 'postprocess_lines'):
                    webui._sofa_prepass(job, config, lambda: False)
                doc = parse_lyrics((directory / 'sofa_lyrics.lrc').read_text(encoding='utf-8'))
                self.assertEqual(len(doc.lines), 1)
                spans = doc.lines[0].word_times
                self.assertAlmostEqual(spans[0][1], start, places=3)
                self.assertAlmostEqual(spans[0][2], start + .4, places=3)
                self.assertAlmostEqual(spans[1][1], start + .4, places=3)
                self.assertAlmostEqual(spans[1][2], end, places=3)
                self.assertEqual(config['lang'], 'ja')
                self.assertEqual(job.sofa_info['refined'], 1)

    def test_non_japanese_prepass_fails_before_audio_or_model_work(self):
        for language, lyrics in [('zh', '中文歌词'), ('en', 'hello'),
                                 ('auto', '中文歌词'), ('auto', 'hello')]:
            with self.subTest(language=language, lyrics=lyrics), \
                    patch.object(webui, '_audio44') as audio:
                with self.assertRaisesRegex(ValueError, '只支持日语'):
                    webui._sofa_prepass(None, {'lang': language, 'lyrics_text': lyrics}, lambda: False)
                audio.assert_not_called()

    def test_direct_backend_rejects_unsupported_language_before_checking_models(self):
        with patch.object(sofa_backend, 'is_available') as available:
            for language in ('zh', 'en', 'auto'):
                with self.subTest(language=language), self.assertRaisesRegex(ValueError, '只支持日语'):
                    sofa_backend.sofa_align_lyrics('missing.wav', ['中文'], Path('unused'),
                                                    language=language)
            available.assert_not_called()


class SofaHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        webui.TASK_QUEUE.join()
        cls.temp = tempfile.TemporaryDirectory()
        cls.old_root = webui.OUT_ROOT
        webui.OUT_ROOT = Path(cls.temp.name)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), webui.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        webui.TASK_QUEUE.join()
        cls.server.shutdown()
        cls.server.server_close()
        webui.OUT_ROOT = cls.old_root
        cls.temp.cleanup()

    def test_api_rejects_explicit_non_japanese_without_queuing(self):
        with patch.object(webui, '_run_job') as worker:
            for language in ('zh', 'en'):
                with self.subTest(language=language):
                    response = requests.post(self.base + '/api/run', timeout=5,
                        files={'media': ('test.wav', b'test')},
                        data={'sofa': '1', 'lyrics_text': 'あい', 'lang': language})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn('日语', response.json()['error'])
            webui.TASK_QUEUE.join()
            worker.assert_not_called()

    def test_api_allows_japanese_and_auto_for_prepass_validation(self):
        configs = []
        def worker(job, config):
            configs.append(config)
            job.state = 'done'
        with patch.object(webui, '_run_job', side_effect=worker):
            for language in ('ja', 'auto'):
                response = requests.post(self.base + '/api/run', timeout=5,
                    files={'media': ('test.wav', b'test')},
                    data={'sofa': '1', 'lyrics_text': 'あい', 'lang': language})
                self.assertEqual(response.status_code, 200, response.text)
            webui.TASK_QUEUE.join()
        self.assertEqual([cfg['lang'] for cfg in configs], ['ja', 'auto'])
        self.assertTrue(all(cfg['sofa_align'] for cfg in configs))


if __name__ == '__main__':
    unittest.main()
