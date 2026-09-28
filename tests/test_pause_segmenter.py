import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np

module_path = Path(__file__).resolve().parents[1] / "core/client/audio/pause_segmenter.py"
spec = importlib.util.spec_from_file_location("pause_segmenter", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
PauseSegmenter = module.PauseSegmenter

SAMPLES = 2400  # 50 ms at 48 kHz


def frame(level):
    return np.full((SAMPLES, 1), level, dtype=np.float32)


class PauseSegmenterTests(unittest.TestCase):
    def test_two_phrases_have_no_shared_speech_or_long_silence(self):
        detector = PauseSegmenter(pause_seconds=0.6)
        result = []
        for level, count in ((0, 6), (0.04, 14), (0, 14),
                             (0.04, 10), (0, 14)):
            for _ in range(count):
                segment = detector.feed(frame(level))
                if segment is not None:
                    result.append(segment)
        self.assertEqual(len(result), 2)
        self.assertEqual(sum(np.count_nonzero(x) for x in result),
                         (14 + 10) * SAMPLES)
        self.assertLess(len(result[0]) / 48000, 1.1)
        self.assertLess(len(result[1]) / 48000, 0.9)
        self.assertIsNone(detector.flush())

    def test_noise_and_short_click_do_not_become_words(self):
        detector = PauseSegmenter(pause_seconds=0.4)
        outputs = []
        for level, count in ((0.001, 20), (0.04, 1), (0.001, 20)):
            for _ in range(count):
                outputs.append(detector.feed(frame(level)))
        self.assertTrue(all(part is None for part in outputs))
        self.assertIsNone(detector.flush())

    def test_release_flushes_last_spoken_phrase(self):
        detector = PauseSegmenter()
        for _ in range(8):
            self.assertIsNone(detector.feed(frame(0.03)))
        part = detector.flush()
        self.assertIsNotNone(part)
        self.assertEqual(np.count_nonzero(part), 8 * SAMPLES)


if __name__ == "__main__":
    unittest.main()
