"""Exercise the real worker loop without a microphone or model inference."""
import os
import queue
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_runtime

class ConversationTests(unittest.TestCase):
    def exercise(self, frames, texts=None, on_reply=None):
        """frames: audio frames, or 'close' for a click on the close button.
        texts: what each transcription returns (default always "give me a summary").
        on_reply(dismissed): runs as Eddie answers, e.g. to click the close button mid-reply."""
        stopped, dismissed = threading.Event(), threading.Event()
        events, replies = [], []
        texts = iter(texts) if texts is not None else None
        def answer(text):
            replies.append(text)
            if on_reply:
                on_reply(dismissed)
        iterator = iter(frames)
        class AudioQueue:
            def __init__(self, **kwargs): pass
            def get(self, **kwargs):
                try: frame = next(iterator)
                except StopIteration:
                    stopped.set()
                    raise queue.Empty
                if isinstance(frame, str):  # 'close': the dashboard's close button
                    dismissed.set()
                    raise queue.Empty
                return frame
            def get_nowait(self): raise queue.Empty
        detector = Mock()
        # Wake on the first two frames only, so a later wake would show up in call_count.
        scores = iter([1.0, 1.0])
        detector.predict.side_effect = lambda frame: {'wake': next(scores, 0.0)}
        transcriber = Mock()
        transcriber.transcribe.side_effect = lambda *a, **k: (
            iter([SimpleNamespace(text=next(texts) if texts else 'give me a summary')]), None)
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        modules = {
            'onnxruntime': SimpleNamespace(disable_telemetry_events=lambda: None),
            'sounddevice': SimpleNamespace(InputStream=Mock(return_value=stream)),
            'faster_whisper': SimpleNamespace(WhisperModel=Mock(return_value=transcriber)),
            'speech_gate': SimpleNamespace(SpeechGate=lambda **kwargs: Mock(side_effect=lambda frame: bool(np.max(frame)>0)), ready_cue=lambda *args: None),
            'wake_detector': SimpleNamespace(VoskWake=Mock(return_value=detector)),
        }
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {'EDDIE_WAKE_ENGINE':'vosk'}), patch.object(Path, 'is_dir', return_value=True), patch.object(voice_runtime.queue, 'Queue', AudioQueue):
            voice_runtime.run(events.append, answer, threading.Event(), stopped, dismissed)
        return events, replies, detector

    def test_initial_silence_dismisses_without_retry(self):
        silence = np.zeros(1280, dtype=np.int16)
        events, replies, detector = self.exercise([silence] * 52)
        self.assertEqual(replies, [])
        self.assertEqual(events[-1], {'type':'voice_state','state':'dismissed','is_eddie_active':False})
        self.assertEqual(detector.predict.call_count, 2)

    def test_followups_do_not_require_another_wake(self):
        silence = np.zeros(1280, dtype=np.int16)
        speech = np.full(1280, 2000, dtype=np.int16)
        events, replies, detector = self.exercise([silence]*2 + [speech] + [silence]*10 + [speech] + [silence]*10 + [silence]*50)
        self.assertEqual(replies, ['give me a summary'] * 2)
        self.assertEqual(detector.predict.call_count, 2)
        self.assertEqual(events[-1]['state'], 'dismissed')

    def states(self, events):
        return [e['state'] for e in events if e.get('type') == 'voice_state']

    def test_noise_twice_in_a_row_ends_the_conversation(self):
        silence = np.zeros(1280, dtype=np.int16)
        noise = np.full(1280, 2000, dtype=np.int16)
        events, replies, _ = self.exercise([silence]*2 + ([noise] + [silence]*10) * 2 + [silence]*5, texts=['', ''])
        self.assertEqual(replies, [''])  # one "Didn't catch that", then nothing
        self.assertEqual(self.states(events), ['listening', 'thinking', 'listening', 'thinking', 'dismissed'])

    def test_a_real_command_after_noise_resets_the_retry(self):
        silence = np.zeros(1280, dtype=np.int16)
        speech = np.full(1280, 2000, dtype=np.int16)
        frames = [silence]*2 + ([speech] + [silence]*10) * 4 + [silence]*50
        _, replies, _ = self.exercise(frames, texts=['', 'give me a summary', '', 'give me a summary'])
        self.assertEqual(replies, ['', 'give me a summary', '', 'give me a summary'])

    def test_close_button_while_eddie_answers_ends_the_conversation(self):
        silence = np.zeros(1280, dtype=np.int16)
        speech = np.full(1280, 2000, dtype=np.int16)
        events, replies, detector = self.exercise([silence]*2 + [speech] + [silence]*10 + [speech] + [silence]*10,
                                                  on_reply=lambda dismissed: dismissed.set())
        self.assertEqual(replies, ['give me a summary'])  # the follow-up speech is not a command
        self.assertEqual(self.states(events)[-1], 'dismissed')
        self.assertNotIn('listening', self.states(events)[1:])
        self.assertGreater(detector.predict.call_count, 2)  # back to waiting for the wake phrase

    def test_close_button_while_listening_ends_the_conversation(self):
        silence = np.zeros(1280, dtype=np.int16)
        events, replies, detector = self.exercise([silence]*3 + ['close'] + [silence]*3)
        self.assertEqual(replies, [])
        self.assertEqual(self.states(events), ['listening', 'dismissed'])
        self.assertEqual(detector.predict.call_count, 5)  # waiting for the wake phrase again
