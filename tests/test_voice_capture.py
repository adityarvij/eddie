import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
from voice_runtime import Capture

class EndpointTests(unittest.TestCase):
    def test_leading_silence_does_not_end_command(self):
        c = Capture()
        for _ in range(20):
            self.assertFalse(c.push(np.zeros(1280, dtype=np.int16)))
        self.assertFalse(c.heard)

    def test_ends_after_speech_then_800ms_silence(self):
        c = Capture()
        self.assertFalse(c.push(np.full(1280, 2000, dtype=np.int16)))
        for _ in range(9):
            self.assertFalse(c.push(np.zeros(1280, dtype=np.int16)))
        self.assertTrue(c.push(np.zeros(1280, dtype=np.int16)))

    def test_no_speech_and_continuous_speech_are_bounded(self):
        for level in (0, 2000):
            c = Capture()
            for _ in range(124):
                self.assertFalse(c.push(np.full(1280, level, dtype=np.int16)))
            self.assertTrue(c.push(np.full(1280, level, dtype=np.int16)))
