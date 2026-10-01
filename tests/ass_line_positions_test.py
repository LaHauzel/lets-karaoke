import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ass_builder import (  # noqa: E402
    AssOptions, KaraokeLine, KaraokeToken, build_ass, parse_line_positions,
)


def sample_lines():
    result = []
    for index, text in enumerate(("first", "second", "third")):
        start = float(index * 2)
        token = KaraokeToken(text=text, disp=text, start=start, end=start + 1)
        result.append(KaraokeLine(raw=text, tokens=[token], start=start, end=start + 1,
                                  ev_start=start, ev_end=start + 1))
    return result


class AssLinePositionsTest(unittest.TestCase):
    def test_each_display_slot_uses_its_own_position(self):
        options = AssOptions(
            line_count=3,
            line_positions=((15, 20), (55, 65), (80, 25)),
        )
        ass = build_ass(sample_lines(), options, width=1920, height=1080)
        self.assertIn(r"{\an2\pos(288,216)}", ass)
        self.assertIn(r"{\an2\pos(1056,702)}", ass)
        self.assertIn(r"{\an2\pos(1536,270)}", ass)

    def test_legacy_global_position_keeps_stacked_defaults(self):
        ass = build_ass(sample_lines(), AssOptions(line_count=2), width=1920, height=1080)
        self.assertIn(r"{\an2\pos(960,961)}", ass)
        self.assertIn(r"{\an2\pos(960,1034)}", ass)

    def test_parse_frontend_positions_and_ignore_invalid_values(self):
        self.assertEqual(parse_line_positions([{"x": 12.5, "y": 88}, [50, 40]]),
                         ((12.5, 88.0), (50.0, 40.0)))
        self.assertIsNone(parse_line_positions([{"x": "bad", "y": 10}]))


if __name__ == "__main__":
    unittest.main()
