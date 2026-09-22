"""Acceptance, bounded retries, reference scoring and real FFmpeg envelopes."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from alignment_review import assess, attach_review
from alignment_retry import retry_lines
from asr_lyrics import AsrLine, Segment
from evaluate_alignment import evaluate, validate
from lyric_waveform import waveform
from pipeline import RestyleRequest, restyle, load_job


def row(text, start, end):
    return {'raw': text, 'start': start, 'end': end,
            'tokens': [{'text': text, 'disp': text, 'start': start, 'end': end}]}


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class AcceptanceTests(unittest.TestCase):
    def test_missing_repeated_occurrence_and_bad_tokens(self):
        aligned = {'lines': [row('repeat', 1, 2), row('end', 3, 4)]}
        result = assess(aligned, ['repeat', 'repeat', 'end'], 5)
        self.assertEqual(result['status'], 'needs_fix')
        self.assertEqual(len(result['missing_input_rows']), 1)
        aligned['lines'][0]['tokens'][0]['end'] = 4
        self.assertIn('invalid_token', [i['code'] for i in assess(aligned, ['repeat', 'end'], 5)['issues']])

    def test_clean_is_spot_check_not_accuracy_claim(self):
        aligned = {'lines': [row('a', 1, 2)], 'diagnostics': {'lines': [{'row': 1, 'confidence': .9}]}}
        self.assertEqual(assess(aligned, ['a'], 3)['status'], 'ready_for_spot_check')
        aligned['diagnostics']['lines'][0]['confidence'] = None
        self.assertEqual(assess(aligned, ['a'], 3)['status'], 'needs_review')

    def test_edits_preserve_missing_but_invalidate_old_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            write(p/'input_lyrics.json', ['a', 'b'])
            evidence = {'lines': [{'row': 1, 'text': 'a', 'start': 1, 'end': 2, 'confidence': .9}]}
            write(p/'alignment_evidence.json', evidence)
            changed = {'base_version': 1, 'lines': [row('a', 1.5, 2.5)], 'review_evidence': evidence}
            result = attach_review(p, changed)
            self.assertIsNone(result['diagnostics']['lines'][0]['confidence'])
            self.assertEqual(result['acceptance']['missing_input_rows'], [{'row': 2, 'text': 'b'}])
            self.assertEqual(result['diagnostics']['missing_input_rows'], [{'row': 2, 'text': 'b'}])

    def test_structural_error_visible_even_with_matching_acoustics(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            write(p/'input_lyrics.json', ['a'])
            write(p/'alignment_evidence.json', {'lines': [{'row': 1, 'text': 'a', 'start': 1, 'end': 2, 'confidence': .9}]})
            bad = row('a', 1, 2)
            bad['tokens'][0]['end'] = 3
            result = attach_review(p, {'lines': [bad]})
            self.assertEqual(result['acceptance']['status'], 'needs_fix')
            self.assertTrue(result['diagnostics']['lines'][0]['reasons'])


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'source.wav'
        self.source.touch()
        self.lines = [AsrLine(c, i*3+1, i*3+2, [Segment(c, i*3+1, i*3+2)], .9 if i != 1 else .3)
                      for i, c in enumerate('abc')]
        self.evidence = {'lines': [{'row': i+1, 'text': x.text, 'start': x.start, 'end': x.end,
                         'confidence': x.prob, 'reasons': ['low'] if i == 1 else []} for i, x in enumerate(self.lines)]}

    def run_retry(self, infer, **kwargs):
        return retry_lines(self.lines, self.evidence, [self.source], 10, 'English', infer=infer, **kwargs)

    def test_accept_keeps_neighbours_exact_and_bounds_window(self):
        calls = []
        def infer(source, text, left, right, *args):
            calls.append((left, right))
            return AsrLine(text, 4.2, 5.2, [Segment(text, 4.2, 5.2)], .8)
        result, evidence, report = self.run_retry(infer)
        self.assertEqual(calls, [(2, 7)])
        self.assertEqual(report['accepted'], 1)
        self.assertIs(result[0], self.lines[0])
        self.assertIs(result[2], self.lines[2])
        self.assertEqual(self.lines[1].start, 4)
        self.assertEqual(evidence['lines'][1]['confidence'], .8)

    def test_reject_text_loss_crossing_overlap_and_no_gain(self):
        for candidate in [AsrLine('wrong', 4.2, 5.2, [Segment('wrong', 4.2, 5.2)], .99),
                          AsrLine('b', 1, 5, [Segment('b', 1, 5)], .99),
                          AsrLine('b', 4, 5.5, [Segment('b', 4, 6)], .99),
                          AsrLine('b', 4.2, 5.2, [Segment('b', 4.2, 5.2)], .35),
                          AsrLine('b', 4, 5, [Segment('b', 4, 5)], .99)]:
            with self.subTest(candidate=candidate):
                result, _, report = self.run_retry(lambda *args: candidate)
                self.assertEqual(report['accepted'], 0)
                self.assertIs(result[1], self.lines[1])

    def test_unsafe_neighbours_do_not_infer(self):
        for risk, score in [(True, .9), (False, None), (False, .2)]:
            self.evidence['lines'][0].update(reasons=['risk'] if risk else [], confidence=score)
            with patch('alignment_retry.infer_clip') as infer:
                _, _, report = self.run_retry(infer)
                self.assertEqual(report['attempted'], 0)
                infer.assert_not_called()

    def test_errors_preserve_results_and_cancel_propagates(self):
        with patch('alignment_retry.infer_clip', side_effect=RuntimeError('inference failed')) as infer:
            result, _, report = self.run_retry(infer)
            self.assertEqual(report['accepted'], 0)
            self.assertEqual(result, self.lines)
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.run_retry(lambda *args: None, cancel=lambda: True)

    def test_budget_three_rows_two_unique_sources(self):
        self.lines = [AsrLine('a', i*2+.5, i*2+1.5, [Segment('a', i*2+.5, i*2+1.5)], .3 if i % 2 == 0 else .9) for i in range(9)]
        self.evidence = {'lines': [{'row': i+1, 'confidence': x.prob, 'reasons': ['risk'] if i % 2 == 0 else []} for i, x in enumerate(self.lines)]}
        sources = [self.source]
        for i in range(3):
            p = self.source.with_name(f'source{i}.wav'); p.touch(); sources.append(p)
        with patch('alignment_retry.infer_clip', return_value=None) as infer:
            _, _, report = retry_lines(self.lines, self.evidence, sources, 20, 'English', infer=infer)
            self.assertEqual(report['attempted'], 3)
            self.assertEqual(infer.call_count, 6)

    def test_restyle_new_version_and_noop(self):
        p = Path(self.temp.name)
        write(p/'job.json', {'job': 'demo', 'media': str(self.source), 'lang': 'en',
                            'media_info': {'duration': 10, 'width': 640, 'height': 360}, 'options': {}})
        write(p/'align.json', {'lines': [row(x.text, x.start, x.end) for x in self.lines]})
        write(p/'alignment_evidence.json', self.evidence)
        write(p/'input_lyrics.json', list('abc'))
        with patch('pipeline.render_video') as render, patch('alignment_retry.infer_clip', return_value=None):
            self.assertTrue(restyle(p, RestyleRequest(retry_suspects=True))['unchanged'])
            render.assert_not_called()
            self.assertFalse((p/'align_v1.json').exists())
        candidate = AsrLine('b', 4.2, 5.2, [Segment('b', 4.2, 5.2)], .8)
        with patch('pipeline.render_video', return_value='mock'), patch('alignment_retry.infer_clip', return_value=candidate):
            result = restyle(p, RestyleRequest(retry_suspects=True))
        aligned = json.loads((p/f"align_v{result['version']}.json").read_text(encoding='utf-8'))
        self.assertEqual(aligned['operation'], 'local_retry')
        reviewed = attach_review(p, aligned)
        self.assertEqual(reviewed['diagnostics']['lines'][1]['confidence'], .8)
        self.assertEqual(load_job(p)[1][1].start, 4)
        self.assertEqual(load_job(p, result['version'])[1][1].start, 4.2)
        self.assertEqual(load_job(p, result['version'])[1][2].start, 7)
        with patch('pipeline.render_video', return_value='mock'):
            later = restyle(p, RestyleRequest(base_version=result['version'], line_bounds={'1': {'start': 4.4, 'end': 5.4}}))
        edited = json.loads((p/f"align_v{later['version']}.json").read_text(encoding='utf-8'))
        self.assertIsNone(attach_review(p, edited)['diagnostics']['lines'][1]['confidence'])


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        examples = Path(__file__).resolve().parents[1]/'docs/examples'
        self.reference = json.loads((examples/'alignment-reference.json').read_text(encoding='utf-8'))
        self.prediction = json.loads((examples/'alignment-prediction.json').read_text(encoding='utf-8'))

    def test_known_errors_by_id_and_ordered_text(self):
        for use_id in (True, False):
            pred = copy.deepcopy(self.prediction)
            if not use_id:
                for p in pred['lines']: p.pop('line_id', None)
            result = evaluate(self.reference, pred)
            self.assertAlmostEqual(result['start']['mae_ms'], 450)
            self.assertEqual(result['timing_review_ids'], ['L002'])
            self.assertEqual(result['missing_rate'], 0)
            self.assertEqual(result['timing_coverage'], 1)

    def test_missing_invalid_and_not_sung_do_not_inflate_coverage(self):
        pred = copy.deepcopy(self.prediction)
        pred['lines'] = [pred['lines'][0], {'line_id': 'L003', 'raw': self.reference['lines'][2]['text'], 'start': 7, 'end': 8}]
        result = evaluate(self.reference, pred)
        self.assertEqual(result['missing_ids'], ['L002'])
        self.assertEqual(result['predicted_not_sung_ids'], ['L003'])
        self.assertEqual(result['uncertain_ids'], ['L004'])
        self.assertEqual(result['timing_coverage'], .5)
        pred['lines'][0]['end'] = float('nan')
        result = evaluate(self.reference, pred)
        self.assertEqual(result['invalid_ids'], ['L001'])
        self.assertEqual(result['start']['n'], 0)

    def test_repeats_with_missing_occurrences_are_ambiguous(self):
        self.reference['lines'][1]['text'] = self.reference['lines'][0]['text']
        pred = {'lines': [dict(self.prediction['lines'][0])]}
        pred['lines'][0].pop('line_id', None)
        result = evaluate(self.reference, pred)
        self.assertEqual(result['ambiguous_repeat_ids'], ['L001', 'L002'])
        self.assertEqual(result['reference_sung_lines'], 2)
        self.assertEqual(result['timing_coverage'], 0)

    def test_invalid_annotation_and_partial_ids_rejected(self):
        self.reference['lines'][2]['start'] = 2
        with self.assertRaises(ValueError): validate(self.reference)
        self.reference['lines'][2]['start'] = None
        self.prediction['lines'][1].pop('line_id')
        with self.assertRaises(ValueError): evaluate(self.reference, self.prediction)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class WaveformTests(unittest.TestCase):
    def test_real_audio_cache_repair_and_source_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); audio = p/'tone.wav'
            with wave.open(str(audio), 'wb') as f:
                f.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                f.writeframes(b'\x00\x10\x00\xf0'*8000)
            write(p/'job.json', {'media': str(audio), 'media_info': {'duration': 2}})
            first = waveform(p)
            self.assertTrue(first['peaks'])
            self.assertLessEqual(len(first['peaks']), 12000)
            self.assertTrue(all(0 <= n <= 1 for n in first['peaks']))
            with patch('lyric_waveform.subprocess.run') as run:
                self.assertEqual(waveform(p), first)
                run.assert_not_called()
            (p/'waveform.json').write_text('{broken', encoding='utf-8')
            self.assertEqual(waveform(p), first)
            audio.unlink()
            with self.assertRaisesRegex(ValueError, '丢失'): waveform(p)


if __name__ == '__main__':
    unittest.main()
