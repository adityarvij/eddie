import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import json

import wake_detector
from wake_detector import matches_phrase

class WakePhrases(unittest.TestCase):
    def test_wake_variants(self):
        for phrase in ('hey eddie', 'Hey Eddy', 'okay edie', 'hey eddie whats going on',
                       'hey andy', 'hey edits'):  # how Vosk heard "hey Eddie" on the Pi's speakerphone
            self.assertTrue(matches_phrase(phrase))

    def test_wake_mid_conversation(self):
        # Vosk partials hold everything since the last final result, so the phrase may follow other speech.
        for phrase in ('so anyway hey eddie', 'they said hey eddie'):
            self.assertTrue(matches_phrase(phrase))

    def test_background_mentions_and_near_matches(self):
        for phrase in ('eddie', 'hey ed', 'hey ed how are you', 'hey editor', 'they eddie', 'ready', 'turn off home mode',
                       'they are the', 'at ease', 'hey', 'andy said hi', 'the edits are done'):
            self.assertFalse(matches_phrase(phrase))


class Recognizer:
    """KaldiRecognizer replaying (final, text) per frame, as heard on the Pi."""
    def __init__(self, frames):
        self.frames = list(frames)

    def AcceptWaveform(self, data):
        self.final, self.text = self.frames.pop(0)
        return self.final

    def Result(self):
        return json.dumps({'text': self.text})

    def PartialResult(self):
        return json.dumps({'partial': self.text})

    def Reset(self):
        pass


class Frame:
    def tobytes(self):
        return b''


def wakes(frames):
    """Whether the runtime's rule (two frames in a row at the threshold) wakes on these frames."""
    detector = object.__new__(wake_detector.VoskWake)
    detector.recognizer, detector.held = Recognizer(frames), 0
    hits = 0
    for _ in frames:
        hits = hits + 1 if detector.predict(Frame())['hey_eddie_prototype'] >= 0.5 else 0
        if hits >= 2:
            return True
    return False


class VoskTiming(unittest.TestCase):
    def test_a_phrase_seen_on_one_frame_still_wakes(self):
        # Only the final result had it ("eighty", then final "hey eddie").
        self.assertTrue(wakes([(False, 'eighty'), (True, 'hey eddie'), (False, '')]))
        # Shown briefly, then revised to something else ("hey eddie" -> "hey edits" before that was added).
        self.assertTrue(wakes([(False, 'hey at'), (False, 'hey eddie'), (True, 'hey eddies'), (False, '')]))

    def test_steady_partials_wake_as_before(self):
        self.assertTrue(wakes([(False, 'hey'), (False, 'hey eddie'), (False, 'hey eddie'), (True, 'hey eddie')]))

    def test_other_speech_does_not(self):
        self.assertFalse(wakes([(False, 'they'), (False, 'they are the'), (True, 'they are the'), (False, '')]))
        self.assertFalse(wakes([(False, 'hey'), (True, 'hey'), (False, 'eddie'), (True, 'eddie')]))

    def test_reset_drops_a_held_match(self):
        detector = object.__new__(wake_detector.VoskWake)
        detector.recognizer, detector.held = Recognizer([(True, 'hey eddie'), (False, '')]), 0
        detector.predict(Frame())
        detector.reset()
        self.assertEqual(0.0, detector.predict(Frame())['hey_eddie_prototype'])


class Grammar(unittest.TestCase):
    def test_every_grammar_phrase_wakes(self):
        for phrase in wake_detector.GRAMMAR:
            if phrase != '[unk]':
                self.assertTrue(matches_phrase(phrase), phrase)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / 'voice' / 'models' / 'vosk-model-small-en-us-0.15').exists(),
                         'needs scripts/provision_voice.py')
    def test_the_model_accepts_the_grammar(self):
        detector = wake_detector.VoskWake(Path(__file__).resolve().parents[1] / 'voice' / 'models' / 'vosk-model-small-en-us-0.15')
        import numpy as np
        self.assertEqual({'hey_eddie_prototype': 0.0}, detector.predict(np.zeros(1280, dtype=np.int16)))
