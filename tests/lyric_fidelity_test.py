"""Preserve supplied timing and explicit lyric revisions without hiding omissions."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from pipeline import PipelineConfig, RestyleRequest, parse_lyrics, restyle, run
from p0_common import TokenSpan
import webui


INFO = {'duration': 10., 'has_video': True, 'has_audio': True,
        'width': 640, 'height': 360, 'fps': 30., 'vcodec': 'h264', 'acodec': 'aac'}


def extract(source, destination, **kwargs):
    Path(destination).touch()
    return str(destination)


class SuppliedTimingTests(unittest.TestCase):
    def test_terminal_empty_marker_closes_word_and_explicit_end_wins(self):
        line = parse_lyrics('[00:01.000]<00:01.000>hello<00:03.000>').lines[0]
        self.assertEqual(line.word_times, [('hello', 1., 3.)])
        line = parse_lyrics('[00:01.000]<00:01.000~00:02.000>hello<00:03.000>').lines[0]
        self.assertEqual(line.word_times, [('hello', 1., 2.)])

    def test_terminal_marker_obeys_offset_and_repeated_line_clocks(self):
        doc = parse_lyrics('[offset:500]\n[00:01.000][00:05.000]<00:01.000>我<00:02.000>')
        self.assertEqual([line.word_times for line in doc.lines],
                         [[('我', .5, 1.5)], [('我', 4.5, 5.5)]])

    def test_unmarked_prefix_uses_the_line_clock_and_first_word_boundary(self):
        line = parse_lyrics('[00:01.000]hello<00:02.000> world<00:03.000>').lines[0]
        self.assertEqual(line.word_times, [('hello', 1., 2.), ('world', 2., 3.)])
        line = parse_lyrics('[00:01.000]hello<00:03.000>').lines[0]
        self.assertEqual(line.word_times, [('hello', 1., 3.)])

    def test_short_positive_word_times_survive_the_complete_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / 'input.mp4'; media.touch()
            config = PipelineConfig(media=str(media),
                lyrics_text='[00:01.000]<00:01.000~00:01.100>我<00:01.500~00:02.500>爱',
                lang='zh', separate=False, vocal_mode='keep', out_dir=tmp, job_name='short')
            with patch('pipeline.probe_media', return_value=INFO), \
                    patch('pipeline.extract_wav', side_effect=extract), \
                    patch('pipeline.load_audio_16k', return_value=np.zeros(160000, dtype=np.float32)), \
                    patch('pipeline.render_video', return_value='x264'), \
                    patch('pipeline.ModelCache.aligner', side_effect=AssertionError('No second alignment')), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = run(config)
            self.assertTrue(result.ok, result.error)
            aligned = json.loads(Path(result.align_json).read_text(encoding='utf-8'))
            self.assertEqual([(t['text'], t['start'], t['end']) for t in aligned['lines'][0]['tokens']],
                             [('我', 1., 1.1), ('爱', 1.5, 2.5)])

    def test_ignored_word_times_use_requested_separation_for_fresh_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'input.mp4'; media.touch()
            vocals, accompaniment = root / 'vocals.wav', root / 'accompaniment.wav'
            vocals.touch(); accompaniment.touch()
            config = PipelineConfig(media=str(media),
                lyrics_text='[00:01.000]<00:01.000~00:02.000>hello', lang='en',
                timed_mode='ignore', separate=True, vocal_mode='keep', out_dir=tmp, job_name='ignore')
            align = Mock(return_value=[TokenSpan('hello', 3., 4.)])
            cache = SimpleNamespace(aligner=lambda *args: SimpleNamespace(align=align))
            with patch('pipeline.probe_media', return_value=INFO), \
                    patch('pipeline.extract_wav', side_effect=extract) as conversion, \
                    patch('pipeline.load_audio_16k', return_value=np.zeros(160000, dtype=np.float32)), \
                    patch('pipeline.render_video', return_value='x264'), \
                    patch('pipeline.separate_stems', return_value=(str(vocals), str(accompaniment))) as separation, \
                    contextlib.redirect_stdout(io.StringIO()):
                result = run(config, cache=cache)
            self.assertTrue(result.ok, result.error)
            separation.assert_called_once()
            align.assert_called_once()
            self.assertTrue(result.stats['separated'])
            self.assertEqual(conversion.call_args_list[-1].args[0], str(vocals))
            aligned = json.loads(Path(result.align_json).read_text(encoding='utf-8'))
            self.assertEqual(aligned['lines'][0]['start'], 3.)

    def test_whisper_sofa_and_asr_prepasses_retain_new_timestamps_when_input_is_ignored(self):
        for route in ('whisper', 'sofa', 'asr'):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                directory = root / 'task'; directory.mkdir()
                media = directory / 'input.wav'; media.touch()
                job = webui.Job('task', directory)
                text = 'あい' if route == 'sofa' else 'hello'
                config = {'media': str(media), 'lyrics_text': '' if route == 'asr' else text,
                          'lang': 'ja' if route == 'sofa' else 'en', 'timed_mode': 'ignore', 'separate': False,
                          'vocal_mode': 'keep', 'device': 'cpu', f'{route}_align': True}
                config['asr_lyrics'] = route == 'asr'
                def prepass(job, cfg, cancel):
                    path = job.dir / 'generated.lrc'
                    path.write_text('[00:01.000]<00:01.000~00:02.000>' + text, encoding='utf-8')
                    cfg['lyrics_path'], cfg['lyrics_text'] = str(path), ''
                def asr_prepass(job, cfg, cancel):
                    (job.dir / 'asr_lyrics.plain.txt').write_text('hello', encoding='utf-8')
                acoustic = Mock(side_effect=AssertionError('New prepass clocks must not be discarded'))
                with patch.object(webui, 'OUT_ROOT', root), \
                        patch.object(webui, '_whisper_prepass', side_effect=prepass), \
                        patch.object(webui, '_sofa_prepass', side_effect=prepass), \
                        patch.object(webui, '_asr_prepass', side_effect=asr_prepass), \
                        patch.object(webui, '_ensure_cache', return_value=SimpleNamespace(aligner=acoustic)), \
                        patch('pipeline.probe_media', return_value=INFO), \
                        patch('pipeline.extract_wav', side_effect=extract), \
                        patch('pipeline.load_audio_16k', return_value=np.zeros(160000, dtype=np.float32)), \
                        patch('pipeline.render_video', return_value='x264'):
                    webui._run_job(job, config)
                self.assertEqual(job.state, 'done', job.error)
                acoustic.assert_not_called()
                self.assertEqual(config['timed_mode'], 'ignore', 'Preserve the original request for audit')
                self.assertEqual(job.result['stats']['mapping']['mode'], 'enhanced-lrc')


class VersionReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fixture(self, expected, actual):
        directory = self.directory
        (directory / 'job.json').write_text(json.dumps({'job': 'test', 'media': 'unused', 'lang': 'en',
            'media_info': INFO, 'options': {}}), encoding='utf-8')
        rows = [{'raw': text, 'start': i * 3 + 1., 'end': i * 3 + 2.,
                 'tokens': [{'text': text, 'disp': text, 'start': i * 3 + 1., 'end': i * 3 + 2.}]}
                for i, text in enumerate(actual)]
        (directory / 'align.json').write_text(json.dumps({'lines': rows}), encoding='utf-8')
        if expected is not None:
            (directory / 'input_lyrics.json').write_text(json.dumps(expected), encoding='utf-8')

    def save(self, request, **kwargs):
        with patch('pipeline.render_video', return_value='x264'), contextlib.redirect_stdout(io.StringIO()):
            return restyle(self.directory, request, **kwargs)

    def acceptance(self, version):
        return json.loads((self.directory / f'acceptance_v{version}.json').read_text(encoding='utf-8'))

    def reference(self, version):
        return json.loads((self.directory / f'align_v{version}.json').read_text(encoding='utf-8'))['acceptance_reference']

    def test_corrected_text_reference_is_versioned_and_original_input_is_immutable(self):
        self.fixture(['helo'], ['helo'])
        original_input = (self.directory / 'input_lyrics.json').read_bytes()
        original_align = (self.directory / 'align.json').read_bytes()
        first = self.save(RestyleRequest(line_texts={0: 'hello'}))
        second = self.save(RestyleRequest(base_version=first['version'], overrides={'font_size': 55}))
        for result in (first, second):
            acceptance = self.acceptance(result['version'])
            self.assertEqual(acceptance['missing_input_rows'], [])
            self.assertNotIn('text_changed', [issue['code'] for issue in acceptance['issues']])
            self.assertEqual(acceptance['reference_source'], 'user_edits')
            self.assertEqual(self.reference(result['version'])['lines'], ['hello'])
        self.assertEqual((self.directory / 'input_lyrics.json').read_bytes(), original_input)
        self.assertEqual((self.directory / 'align.json').read_bytes(), original_align)

    def test_output_row_mapping_does_not_overwrite_a_missing_original_row(self):
        self.fixture(['alpha', 'beta', 'omega'], ['alpha', 'omega'])
        result = self.save(RestyleRequest(line_texts={1: 'omega corrected'}))
        self.assertEqual(self.reference(result['version'])['lines'], ['alpha', 'beta', 'omega corrected'])
        self.assertEqual(self.acceptance(result['version'])['missing_input_rows'], [{'row': 2, 'text': 'beta'}])

    def test_old_version_chain_recovers_only_recorded_lyric_intents(self):
        self.fixture(['helo', 'missing', 'end'], ['helo', 'end'])
        rows = json.loads((self.directory / 'align.json').read_text())['lines']
        rows[0]['raw'] = 'hello'
        rows[0]['tokens'][0].update(text='hello', disp='hello')
        (self.directory / 'align_v1.json').write_text(json.dumps({
            'version': 1, 'base_version': 0, 'line_texts': {'0': 'hello'}, 'lines': rows}), encoding='utf-8')
        (self.directory / 'align_v2.json').write_text(json.dumps({
            'version': 2, 'base_version': 1, 'lines': rows}), encoding='utf-8')
        for version in (1, 2):
            (self.directory / f'karaoke_v{version}.ass').touch()
        result = self.save(RestyleRequest(base_version=2))
        self.assertEqual(self.reference(result['version'])['lines'], ['hello', 'missing', 'end'])
        self.assertEqual(self.acceptance(result['version'])['missing_input_rows'], [{'row': 2, 'text': 'missing'}])
        self.assertEqual(json.loads((self.directory / 'input_lyrics.json').read_text()), ['helo', 'missing', 'end'])

    def test_inserting_a_missing_input_row_does_not_duplicate_its_reference(self):
        self.fixture(['alpha', 'beta', 'omega'], ['alpha', 'omega'])
        result = self.save(RestyleRequest(line_insertion={
            'after_row': 0, 'text': 'beta', 'mode': 'manual', 'start': 2.2, 'end': 2.8}))
        self.assertEqual(self.reference(result['version'])['lines'], ['alpha', 'beta', 'omega'])
        self.assertEqual(self.acceptance(result['version'])['missing_input_rows'], [])

    def test_restoring_a_missing_repeat_does_not_add_a_fourth_expected_occurrence(self):
        self.fixture(['repeat', 'repeat', 'repeat'], ['repeat', 'repeat'])
        result = self.save(RestyleRequest(line_insertion={
            'after_row': 0, 'text': 'repeat', 'mode': 'manual', 'start': 2.5, 'end': 3.5}))
        self.assertEqual(self.reference(result['version'])['lines'], ['repeat'] * 3)
        self.assertEqual(self.acceptance(result['version'])['missing_input_rows'], [])

    def test_new_lyric_is_added_only_from_an_explicit_insertion(self):
        self.fixture(['alpha', 'omega'], ['alpha', 'omega'])
        result = self.save(RestyleRequest(line_insertion={
            'after_row': 0, 'text': 'new lyric', 'mode': 'manual', 'start': 2.2, 'end': 3.2}))
        self.assertEqual(self.reference(result['version'])['lines'], ['alpha', 'new lyric', 'omega'])
        self.assertEqual(self.acceptance(result['version'])['missing_input_rows'], [])
        self.assertEqual(json.loads((self.directory / 'input_lyrics.json').read_text()), ['alpha', 'omega'])

    def test_multiple_drafts_keep_explicit_edits_and_still_report_original_omissions(self):
        self.fixture(['helo', 'missing', 'end'], ['helo', 'end'])
        first = self.save(RestyleRequest(preview_only=True, line_texts={0: 'hello'}, line_insertion={
            'after_row': 0, 'text': 'new', 'mode': 'manual', 'start': 2.2, 'end': 2.8}))
        second = self.save(RestyleRequest(preview_only=True, line_texts={1: 'newer'}, line_insertion={
            'after_row': 1, 'text': 'another', 'mode': 'manual', 'start': 3., 'end': 3.6}),
            initial_lines=first['_draft_lines'], initial_reference=first['acceptance_reference'])
        final = self.save(RestyleRequest(), initial_lines=second['_draft_lines'],
                          initial_reference=second['acceptance_reference'])
        self.assertEqual(self.reference(final['version'])['lines'], ['hello', 'newer', 'another', 'missing', 'end'])
        self.assertEqual(self.acceptance(final['version'])['missing_input_rows'], [{'row': 4, 'text': 'missing'}])
        self.assertEqual(json.loads((self.directory / 'input_lyrics.json').read_text()), ['helo', 'missing', 'end'])

    def test_missing_original_input_never_uses_the_output_as_its_own_reference(self):
        self.fixture(None, ['helo'])
        result = self.save(RestyleRequest(line_texts={0: 'hello'}))
        self.assertIsNone(self.reference(result['version']))
        self.assertIn('input_unknown', [issue['code'] for issue in self.acceptance(result['version'])['issues']])


if __name__ == '__main__':
    unittest.main()
