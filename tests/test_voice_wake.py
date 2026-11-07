import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
from wake_detector import matches_phrase

class WakePhrases(unittest.TestCase):
    def test_wake_variants(self):
        for phrase in ('hey eddie', 'Hey Eddy', 'okay edie', 'hey eddie whats going on'):
            self.assertTrue(matches_phrase(phrase))

    def test_wake_mid_conversation(self):
        # Vosk partials hold everything since the last final result, so the phrase may follow other speech.
        for phrase in ('so anyway hey eddie', 'they said hey eddie'):
            self.assertTrue(matches_phrase(phrase))

    def test_background_mentions_and_near_matches(self):
        for phrase in ('eddie', 'hey ed', 'hey ed how are you', 'hey editor', 'they eddie', 'ready', 'turn off home mode'):
            self.assertFalse(matches_phrase(phrase))
