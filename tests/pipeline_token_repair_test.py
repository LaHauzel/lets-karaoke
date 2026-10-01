import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ass_builder import KaraokeLine, KaraokeToken
from pipeline import _repair_zero_line_tokens


class TokenRepairTests(unittest.TestCase):
    def test_collapsed_character_borrows_only_local_time(self):
        tokens = [KaraokeToken('a', 'a', 1.0, 1.1),
                  KaraokeToken('b', 'b', 1.1, 1.1),
                  KaraokeToken('c', 'c', 1.1, 1.2),
                  KaraokeToken('d', 'd', 1.5, 1.8)]
        line = KaraokeLine('abcd', tokens, start=1.0, end=1.8)
        self.assertEqual(_repair_zero_line_tokens(line), 1)
        self.assertTrue(all(t.end > t.start for t in tokens))
        self.assertEqual((tokens[0].start, tokens[2].end, tokens[3].start),
                         (1.0, 1.2, 1.5))

    def test_does_not_fill_long_gap_as_singing(self):
        tokens = [KaraokeToken('a', 'a', 1.0, 1.01),
                  KaraokeToken('b', 'b', 1.01, 1.01),
                  KaraokeToken('c', 'c', 2.0, 2.01)]
        line = KaraokeLine('abc', tokens, start=1.0, end=2.01)
        self.assertEqual(_repair_zero_line_tokens(line), 0)
        self.assertEqual(tokens[1].start, tokens[1].end)


if __name__ == '__main__':
    unittest.main()
