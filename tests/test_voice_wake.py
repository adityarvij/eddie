import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
from wake_detector import matches_phrase

class WakePhrases(unittest.TestCase):
    def test_wake_variants(self):
        for phrase in ('hey eddie', 'Hey Eddy', 'hey eddie whats going on'):
            self.assertTrue(matches_phrase(phrase))

    def test_background_mentions_and_near_matches(self):
        for phrase in ('eddie', 'hey editor', 'they said hey eddie', 'ready', 'turn off home mode'):
            self.assertFalse(matches_phrase(phrase))
