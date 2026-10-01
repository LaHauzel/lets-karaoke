"""Regression cases for lyric formats, mixed scripts and acoustic windows."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import numpy as np
from align_backends import _merge_owned_spans
from ass_builder import AssOptions, KaraokeLine, KaraokeToken, _fmt_time, _srt_time, build_ass, check_windows, solve_windows
from pipeline import ModelCache, _ass_opt_from, build_display, parse_lyrics
from p0_common import LANGS, alignment_text, tokenize
import sofa_backend
from whisper_align import DEFAULT_RULES, build_lines, group_words_to_lines
from alignment_policy import resolve_rules


def lyric(text, start, end):
    return KaraokeLine(text, [KaraokeToken(text, text, start, end)], start=start, end=end)


class FormatTests(unittest.TestCase):
    def test_signed_lrc_offset_applies_to_lines_and_enhanced_words(self):
        for offset, expected in [(1000, 1), (-1000, 3)]:
            doc = parse_lyrics(f'[offset:{offset}]\n[00:02.000]<00:02.000~00:03.000>你')
            self.assertEqual(doc.lines[0].start, expected)
            self.assertEqual(doc.lines[0].word_times, [('你', expected, expected + 1)])
        self.assertEqual(parse_lyrics('[offset:3000]\n[00:02.000]你').lines[0].start, 0)

    def test_last_offset_is_global_and_repeated_enhanced_line_gets_own_clock(self):
        doc = parse_lyrics('[offset:500]\n[00:02.000][00:10.000]<00:02.000~00:03.000>你\n[offset:1000]')
        self.assertEqual([line.start for line in doc.lines], [1, 9])
        self.assertEqual([line.word_times for line in doc.lines], [[('你', 1, 2)], [('你', 9, 10)]])

    def test_multiline_srt_stays_in_one_ass_event(self):
        doc = parse_lyrics('1\n00:00:01,000 --> 00:00:03,000\nALPHA\nBETA\n')
        text = doc.lines[0].text
        words = tokenize(text, LANGS['en'])
        head, displays = build_display(text, words)
        line = KaraokeLine(text, [KaraokeToken(word, displays[i], 1+i, 2+i)
                                  for i, word in enumerate(words)], head=head, start=1, end=3)
        opt = AssOptions(next_line=False)
        solve_windows([line], opt)
        events = [row for row in build_ass([line], opt).splitlines() if row.startswith('Dialogue:')]
        self.assertEqual(len(events), 1)
        self.assertIn(r'ALPHA\N', events[0])
        self.assertIn('BETA', events[0])

    def test_zero_padding_values_survive_option_reload(self):
        opt = _ass_opt_from({'lead_ms': 0, 'tail_ms': 0, 'min_gap_ms': 0, 'margin_v': 0,
                             'position_x': 0, 'position_y': 0})
        self.assertEqual((opt.lead_ms, opt.tail_ms, opt.min_gap_ms, opt.margin_v,
                          opt.position_x, opt.position_y), (0, 0, 0, 0, 0, 0))

    def test_timestamp_rounding_carries_into_minutes_and_hours(self):
        self.assertEqual(_fmt_time(59.999), '0:01:00.00')
        self.assertEqual(_fmt_time(3599.999), '1:00:00.00')
        self.assertEqual(_srt_time(59.9999), '00:01:00,000')
        self.assertEqual(_srt_time(3599.9999), '01:00:00,000')


class TimingTests(unittest.TestCase):
    def test_previews_yield_to_contiguous_singing_and_preserve_held_tail(self):
        lines = [lyric('held', 1, 5), lyric('next', 5, 7), lyric('last', 7.1, 8)]
        solve_windows(lines, AssOptions())
        self.assertEqual(lines[0].ev_end, 5)
        self.assertEqual(lines[1].ev_start, 5)
        self.assertTrue(check_windows(lines)['ok'])
        for line in lines:
            self.assertLessEqual(line.ev_start, line.start)
            self.assertGreaterEqual(line.ev_end, line.end)

    def test_real_overlapping_singers_are_reported_instead_of_chopping_tails(self):
        lines = [lyric('a', 1, 5), lyric('b', 4, 6)]
        solve_windows(lines, AssOptions())
        self.assertEqual(lines[0].ev_end, 5)
        self.assertEqual(lines[1].ev_start, 4)
        self.assertFalse(check_windows(lines)['ok'])
        self.assertEqual(check_windows(lines)['overlap_pairs'], [0])

    def test_health_detects_a_clipped_singing_interval(self):
        line = lyric('tail', 1, 3)
        line.ev_start, line.ev_end = .7, 2.7
        self.assertEqual(check_windows([line])['clipped_singing_lines'], [0])
        self.assertFalse(check_windows([line])['ok'])

    def test_short_interjection_does_not_push_next_acoustic_anchor(self):
        lines = [lyric('a', 0, .02), lyric('b', .02, .04)]
        solve_windows(lines, AssOptions(lead_ms=0, tail_ms=0))
        self.assertEqual(lines[0].ev_end, .02)
        self.assertEqual(lines[1].ev_start, .02)
        self.assertTrue(check_windows(lines)['ok'])


class TokenTests(unittest.TestCase):
    def test_mixed_chinese_english_numbers_and_apostrophe_are_timed(self):
        tokens = tokenize("我 love you 2026 don't 忘记", LANGS['zh'])
        self.assertEqual(tokens, ['我', 'love', 'you', '2026', "don't", '忘', '记'])
        self.assertEqual(alignment_text(tokens, 'zh'), "我love you 2026 don't忘记")
        head, displays = build_display("我 love you 2026 don't 忘记", tokens)
        self.assertEqual(head + ''.join(displays), "我 love you 2026 don't 忘记")

    def test_japanese_long_vowels_and_foreign_words_are_preserved(self):
        self.assertEqual(tokenize('スーパー LOVE １２３', LANGS['ja']),
                         ['ス', 'ー', 'パ', 'ー', 'LOVE', '123'])

    def test_repeated_ctc_owners_remain_distinct_but_subcharacters_merge(self):
        frames = [SimpleNamespace(start=i*10, end=i*10+8) for i in range(4)]
        result = _merge_owned_spans(frames, [0, 0, 1, 1], ['again', 'again'], .02)
        self.assertEqual(len(result), 2)
        self.assertEqual([(s.start, s.end) for s in result], [(0, .36), (.4, .76)])
        self.assertEqual([s.unit for s in result], [0, 1])

    def test_ctc_oov_skip_does_not_merge_equal_occurrences(self):
        frames = [SimpleNamespace(start=0, end=10), SimpleNamespace(start=20, end=30)]
        result = _merge_owned_spans(frames, [0, 2], ['人', 'missing', '人'], .02)
        self.assertEqual([s.text for s in result], ['人', '人'])
        self.assertEqual(result[1].start, .4)

    def test_multilingual_qwen_cache_reuses_one_model(self):
        from concurrent.futures import ThreadPoolExecutor
        cache = ModelCache('cpu')
        with patch('align_backends.QwenAligner', side_effect=lambda **kwargs: object()) as load:
            with ThreadPoolExecutor(max_workers=3) as pool:
                models = list(pool.map(lambda lang: cache.aligner('qwen', lang), ['zh', 'en', 'ja']))
        self.assertTrue(all(model is models[0] for model in models))
        self.assertEqual(load.call_count, 1)

    def test_asr_language_wrappers_share_weights_without_changing_prior_language(self):
        cache = ModelCache('cpu')
        with patch('asr_lyrics.LocalAsr', side_effect=lambda **kwargs: SimpleNamespace(model=object(), **kwargs)) as load:
            japanese, english = cache.asr('ja'), cache.asr('en')
        self.assertIs(japanese.model, english.model)
        self.assertEqual((japanese.language, english.language), ('Japanese', 'English'))
        self.assertEqual(load.call_count, 1)


class WhisperRowsTests(unittest.TestCase):
    def test_one_model_unit_crossing_line_break_is_split_without_losing_row(self):
        words = [['hello world', 0, 2, .9]]
        groups, covered = group_words_to_lines(words, ['hello', 'world'])
        self.assertEqual(covered, 1)
        self.assertEqual([group[0][0].strip() for group in groups], ['hello', 'world'])
        self.assertEqual([(group[0][1], group[0][2]) for group in groups], [(0, 1), (1, 2)])
        rows, report = build_lines(words, ['hello', 'world'], resolve_rules(DEFAULT_RULES))
        self.assertEqual(report['row_index'], [0, 1])
        self.assertEqual([row.text for row in rows], ['hello', 'world'])
        self.assertEqual(words, [['hello world', 0, 2, .9]])

    def test_punctuation_difference_does_not_shift_subsequent_rows(self):
        words = [['hello,', 0, 1, .9], ['world!', 2, 3, .9], ['hello', 4, 5, .9]]
        groups, _ = group_words_to_lines(words, ['hello', 'world', 'hello'])
        self.assertEqual([group[0][0] for group in groups], ['hello,', 'world!', 'hello'])
        self.assertEqual([group[0][1] for group in groups], [0, 2, 4])

    def test_missing_row_does_not_claim_later_matching_words(self):
        groups, covered = group_words_to_lines([['alpha', 1, 2, .9], ['omega', 5, 6, .9]],
                                                ['alpha', 'beta', 'omega'])
        self.assertEqual(covered, 2)
        self.assertEqual(groups[1], [])
        self.assertEqual(groups[2][0][0], 'omega')


class SofaTests(unittest.TestCase):
    def phonemes(self, text):
        fake = SimpleNamespace(kakasi=lambda: SimpleNamespace(convert=lambda value: [{'orig': value, 'kana': value}]))
        with patch.dict(sys.modules, {'pykakasi': fake}):
            return sofa_backend.to_phonemes(text)

    def test_base_vowels_particles_and_small_vowels(self):
        self.assertEqual(self.phonemes('あいうえお')[0], list('aiueo'))
        self.assertEqual(self.phonemes('アイウエオ')[0], list('aiueo'))
        self.assertEqual(self.phonemes('を')[0], ['o'])
        self.assertEqual(self.phonemes('ァィゥェォ')[0], list('aiueo'))

    def test_sokuon_owns_only_the_extra_consonant(self):
        phones, owners = self.phonemes('がっこう')
        self.assertEqual(phones, ['g', 'a', 'k', 'k', 'o', 'u'])
        self.assertEqual(owners, [0, 0, 1, 2, 2, 3])

    def test_long_vowel_and_foreign_v_sound(self):
        self.assertEqual(self.phonemes('スーパー')[0], ['s', 'u', 'u', 'p', 'a', 'a'])
        self.assertEqual(self.phonemes('ヴァ')[0], ['v', 'a'])

    @unittest.skipUnless(importlib.util.find_spec('pykakasi'), 'Optional SOFA dependency')
    def test_actual_kanji_reading_preserves_standalone_vowels(self):
        self.assertEqual(sofa_backend.to_phonemes('愛')[0], ['a', 'i'])

    def test_direct_backend_reaches_process_runner_and_forwards_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            wav, dictionary = folder/'voice.wav', folder/'dictionary.txt'
            wav.write_bytes(b'wav'); dictionary.write_text('', encoding='utf-8')
            cancel = lambda: False
            with patch('sofa_backend.is_available', return_value=(True, '')), \
                    patch('sofa_backend.JA_DICT', dictionary), \
                    patch('sofa_backend.to_phonemes', return_value=(['a','i'], [0,1])), \
                    patch('sofa_backend._parse_textgrid', return_value=[(0,.5,'a'),(.5,1,'i'),(1,1.2,'SP')]), \
                    patch('sofa_backend.run_process', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as process:
                result = sofa_backend.sofa_align_lyrics(str(wav), ['あい'], folder/'work', cancel=cancel)
            self.assertEqual(len(result), 1)
            self.assertIs(process.call_args.kwargs['cancel'], cancel)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg/libass required')
class RenderRegressionTests(unittest.TestCase):
    def test_real_libass_renders_both_srt_text_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            text = parse_lyrics('1\n00:00:00,000 --> 00:00:03,000\nALPHA\nBETA\n').lines[0].text
            words = tokenize(text, LANGS['en'])
            head, displays = build_display(text, words)
            line = KaraokeLine(text, [KaraokeToken(word, displays[i], i, i+1)
                                      for i, word in enumerate(words)], head=head, start=0, end=3)
            opt = AssOptions(font_size=70, next_line=False, position_y=80)
            solve_windows([line], opt)
            (folder/'test.ass').write_text(build_ass([line], opt, 640, 360), encoding='utf-8-sig')
            result = subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=640x360:r=1:d=3',
                                     '-vf','ass=test.ass','-ss','1','-frames:v','1','-pix_fmt','rgb24',
                                     '-f','rawvideo','-'], cwd=folder, capture_output=True, check=True, timeout=30)
            pixels = np.frombuffer(result.stdout, dtype=np.uint8).reshape(360, 640, 3)
            rows = np.flatnonzero((pixels.max(axis=2) > 100).sum(axis=1) > 5)
            bands = 1 + np.count_nonzero(np.diff(rows) > 3) if len(rows) else 0
            self.assertGreaterEqual(bands, 2, 'Both original SRT rows must be visible')


if __name__ == '__main__':
    unittest.main()
