import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ass_builder import AssOptions, KaraokeLine, KaraokeToken, build_ass, display_chunks, gap_hints, solve_windows


class AssGapTest(unittest.TestCase):
    def test_scan_ends_at_voice_end_and_dots_fill_long_silence(self):
        first = KaraokeLine(tokens=[KaraokeToken('A', 'A', 1., 2.)], start=1., end=2.)
        second = KaraokeLine(tokens=[KaraokeToken('B', 'B', 15., 16.)], start=15., end=16.)
        options = AssOptions(next_line=False)
        solve_windows([first, second], options)
        ass = build_ass([first, second], options)
        self.assertIn(r'{\kf100}A', ass)
        self.assertIn('0:00:02.32,LYRIC', ass)
        hints = list(gap_hints([first, second], options))
        self.assertEqual([x[2] for x in hints[:3]], ['.', '. .', '. . .'])
        self.assertAlmostEqual(hints[0][0], first.ev_end)
        self.assertAlmostEqual(hints[-1][1], second.ev_start)
        self.assertTrue(all(first.end <= a < b <= second.start for a, b, _ in hints))

    def test_pause_inside_input_row_hides_previous_phrase(self):
        line = KaraokeLine(tokens=[KaraokeToken('A', 'A ', 1., 2.),
                                   KaraokeToken('B', 'B', 15., 16.)], start=1., end=16.)
        chunks = display_chunks([line], AssOptions())
        self.assertEqual([(x.start, x.end) for x in chunks], [(1., 2.), (15., 16.)])
        self.assertEqual(line.end, 16.)  # The evidence is not rewritten by rendering.
        ass = build_ass([line], AssOptions(next_line=False))
        self.assertEqual(len(re.findall(r',LYRIC,,', ass)), 2)
        self.assertIn(',GAP,,', ass)

    def test_short_pause_and_final_outro_have_no_waiting_hint(self):
        line = KaraokeLine(tokens=[KaraokeToken('A', 'A', 1., 2.)], start=1., end=2.)
        solve_windows([line], AssOptions())
        self.assertEqual(list(gap_hints([line], AssOptions())), [])


if __name__ == '__main__':
    unittest.main()
