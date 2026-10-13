"""Validate the portable CTC path against exhaustive small alignments."""
import itertools
import contextlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from types import ModuleType, SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from align_backends import Wav2Vec2Aligner, _ctc_spans, _merge_owned_spans


def runs(path, blank):
    result = []
    for index, label in enumerate(path):
        if index and label == path[index - 1]:
            if label != blank:
                result[-1][2] = index + 1
        elif label != blank:
            result.append([label, index, index + 1])
    return result


class CtcAlignmentTests(unittest.TestCase):
    def test_importable_api_without_registered_kernel_uses_portable_path(self):
        class Tensor:
            def __init__(self, values): self.values = np.asarray(values)
            @property
            def shape(self): return self.values.shape
            def to(self, device): return self
            def float(self): return self
            def numpy(self): return self.values
            def __getitem__(self, index): return Tensor(self.values[index])
        torch = ModuleType('torch')
        torch.no_grad = contextlib.nullcontext
        torch.log_softmax = lambda tensor, dim: tensor
        torch.ops = SimpleNamespace(torchaudio=SimpleNamespace())
        audio_api = ModuleType('torchaudio.functional')
        def unavailable(*args, **kwargs):
            raise AssertionError('The importable API has no native operator')
        audio_api.forced_align = audio_api.merge_tokens = unavailable
        aligner = Wav2Vec2Aligner.__new__(Wav2Vec2Aligner)
        aligner.device, aligner.blank = 'cpu', 0
        aligner.encode = lambda tokens: ([1, 1], [0, 1])
        aligner.fe = lambda audio, **kw: SimpleNamespace(input_values=Tensor([audio]))
        scores = np.log([[[.05, .95], [.95, .05], [.05, .95]]])
        aligner.model = lambda audio: SimpleNamespace(logits=Tensor(scores))
        with patch.dict(sys.modules, {'torch':torch, 'torchaudio':ModuleType('torchaudio'),
                                      'torchaudio.functional':audio_api}):
            spans = aligner.align(np.zeros(16000), ['again', 'again'], 'en')
        self.assertEqual([s.unit for s in spans], [0, 1])
        self.assertAlmostEqual(spans[0].end, 1/3)
        self.assertAlmostEqual(spans[1].start, 2/3)
        self.assertAlmostEqual(spans[1].end, 1)

    def test_matches_exhaustive_viterbi_with_repeats_and_nonzero_blank(self):
        random = np.random.default_rng(73)
        for blank, targets in [(0, [1]), (0, [1, 2]), (0, [1, 1]), (0, [2, 1, 2]), (2, [0, 0, 1])]:
            for _ in range(4):
                probabilities = random.normal(size=(5, 3))
                candidates = [path for path in itertools.product(range(3), repeat=5)
                              if [r[0] for r in runs(path, blank)] == targets]
                best = max(candidates, key=lambda p: sum(probabilities[i, label] for i, label in enumerate(p)))
                expected = [(r[1], r[2]) for r in runs(best, blank)]
                spans = _ctc_spans(probabilities, targets, blank)
                self.assertEqual([(s.start, s.end) for s in spans], expected)

    def test_repeated_token_occurrences_do_not_merge_after_frame_conversion(self):
        probabilities = np.log([[.05, .95], [.95, .05], [.05, .95]])
        spans = _ctc_spans(probabilities, [1, 1])
        result = _merge_owned_spans(spans, [0, 1], ['again', 'again'], .02)
        self.assertEqual([(s.start, s.end, s.unit) for s in result], [(0, .02, 0), (.04, .06, 1)])

    def test_invalid_or_impossible_paths_fail_instead_of_inventing_timings(self):
        with self.assertRaisesRegex(RuntimeError, '帧数不足'):
            _ctc_spans(np.zeros((2, 2)), [1, 1])
        with self.assertRaisesRegex(RuntimeError, '有效对齐路径'):
            _ctc_spans(np.array([[0., -np.inf]] * 3), [1])
        for probabilities, targets in [(np.zeros((3, 2)), [0]), (np.zeros((3, 2)), [2]),
                                       (np.array([[np.nan, 0]]), [1])]:
            with self.assertRaises(ValueError):
                _ctc_spans(probabilities, targets)
        self.assertEqual(_ctc_spans(np.zeros((3, 2)), []), [])


if __name__ == '__main__':
    unittest.main()
