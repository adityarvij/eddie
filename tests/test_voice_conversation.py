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
    def exercise(self, frames):
        stopped = threading.Event()
        events, replies = [], []
        iterator = iter(frames)
        class AudioQueue:
            def __init__(self, **kwargs): pass
            def get(self, **kwargs):
                try: return next(iterator)
                except StopIteration:
                    stopped.set()
                    raise queue.Empty
            def get_nowait(self): raise queue.Empty
        detector = Mock()
        detector.predict.return_value = {'wake': 1.0}
        transcriber = Mock()
        transcriber.transcribe.side_effect = lambda *a, **k: (iter([SimpleNamespace(text='give me a summary')]), None)
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        modules = {
            'onnxruntime': SimpleNamespace(disable_telemetry_events=lambda: None),
            'sounddevice': SimpleNamespace(InputStream=Mock(return_value=stream)),
            'faster_whisper': SimpleNamespace(WhisperModel=Mock(return_value=transcriber)),
            'wake_detector': SimpleNamespace(VoskWake=Mock(return_value=detector)),
        }
        with patch.dict(sys.modules, modules), patch.dict(os.environ, {'EDDIE_WAKE_ENGINE':'vosk'}), patch.object(Path, 'is_dir', return_value=True), patch.object(voice_runtime.queue, 'Queue', AudioQueue):
            voice_runtime.run(events.append, replies.append, threading.Event(), stopped)
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
