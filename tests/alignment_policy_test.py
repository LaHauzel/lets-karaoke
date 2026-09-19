"""Generalization/constraint regressions, no GPU or copyrighted text required."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from alignment_policy import bounded_refine, project_timeline, resolve_rules
from asr_lyrics import AsrLine, Segment
from whisper_align import DEFAULT_RULES, postprocess_lines, merge_lines_monotonic


def line(start, end, prob=.9):
    return AsrLine('a', start, end, [Segment('a', start, end, prob)], prob)


class PolicyTests(unittest.TestCase):
    def test_balanced_tail_constraint_runs_after_minimum_duration(self):
        lines = [line(0, .5, .9), line(.51, .55, .9)]
        postprocess_lines(lines, [(0, .7)],
                          {'min_line_dur':1, 'tail_extend':False, 'start_snap':False}, 'balanced')
        self.assertTrue(all(l.end <= .7 + 1e-9 for l in lines))

    def test_joint_selection_avoids_crossing_between_choruses(self):
        first = [line(10, 12, .9), line(13, 15, .6)]
        other = [line(30, 32, .99), line(13, 15, .7)]
        merged, report = merge_lines_monotonic(first, [0,1], other, [0,1], 2)
        self.assertEqual(merged[0].start, 10)
        self.assertEqual(merged[1].prob, .7)
        self.assertEqual(report['unavoidable_reversals'], 0)

    def test_held_note_preserved(self):
        lines = [line(1, 8), line(10, 11)]
        postprocess_lines(lines, [(1, 8), (10, 11)])
        self.assertEqual(lines[0].end, 8)

    def test_no_evidence_does_not_delete_or_relocate(self):
        lines = [line(20, 24, .01)]
        result = postprocess_lines(lines, [(1, 10)])
        self.assertEqual((lines[0].start, lines[0].end), (20, 24))
        self.assertEqual(result['uncertain'], [0])

    def test_far_boundary_does_not_rescale(self):
        lines = [line(1, 2)]
        postprocess_lines(lines, [(1, 9)])
        self.assertAlmostEqual(lines[0].end, 2)

    def test_nearby_boundary_and_zero_override(self):
        lines = [line(1, 2)]
        postprocess_lines(lines, [(1, 2.4)])
        self.assertEqual(lines[0].end, 2.4)
        lines = [line(1, 2)]
        postprocess_lines(lines, [(1, 2.4)], {'boundary_limit': 0})
        self.assertAlmostEqual(lines[0].end, 2)

    def test_projection_orders_collapsed_and_backward_spans(self):
        lines = [line(5, 8, .01), line(4, 4, .01), line(7, 8, .99)]
        result = project_timeline(lines)
        for a, b in zip(lines, lines[1:]):
            self.assertLessEqual(a.end, b.start + 1e-9)
        for item in lines:
            self.assertGreaterEqual(item.end - item.start, .01 - 1e-9)
        self.assertTrue(result['changed_rows'])
        snapshot = copy.deepcopy(lines)
        project_timeline(lines)
        self.assertEqual(lines, snapshot)

    def test_later_confident_anchor_resists_earlier_bad_estimate(self):
        lines = [line(1, 20, .01), line(10, 11, .99)]
        project_timeline(lines)
        self.assertLess(abs(lines[1].start - 10), .2)

    def test_invalid_parameters_rejected(self):
        for bad in ({'conf_low': float('nan')}, {'conf_low': 2},
                    {'tail_extend': 'false'}, {'unknown': 1}, [],
                    {'conf_low': .9, 'conf_high': .5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_rules(DEFAULT_RULES, bad)


if __name__ == '__main__':
    unittest.main()
