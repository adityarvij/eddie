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

    def test_no_speech_ends_after_4_seconds(self):
        c = Capture()
        for _ in range(49):
            self.assertFalse(c.push(np.zeros(1280, dtype=np.int16)))
        self.assertTrue(c.push(np.zeros(1280, dtype=np.int16)))
        self.assertFalse(c.heard)
        self.assertLessEqual(len(c.frames), 3)

    def test_late_speech_gets_full_command_window(self):
        c = Capture()
        for _ in range(49):
            c.push(np.zeros(1280, dtype=np.int16))
        for _ in range(124):
            self.assertFalse(c.push(np.full(1280, 2000, dtype=np.int16)))
        self.assertTrue(c.push(np.full(1280, 2000, dtype=np.int16)))
        self.assertTrue(c.heard)


class TranscriptionRecoveryTests(unittest.TestCase):
    def test_empty_and_failed_transcriptions_prompt_retry(self):
        from unittest.mock import Mock
        from voice_runtime import transcribe_command
        for failure in (False, True):
            transcriber = Mock()
            if failure:
                transcriber.transcribe.side_effect = RuntimeError('unavailable')
            else:
                transcriber.transcribe.return_value = (iter([]), None)
            emit, answer = Mock(), Mock()
            transcribe_command(np.zeros(1280), transcriber, emit, answer)
            answer.assert_called_once_with('')
            self.assertFalse(any(call.args[0].get('type') == 'transcript' for call in emit.call_args_list))

    def test_successful_transcription_dispatches_exact_command(self):
        from unittest.mock import Mock
        from types import SimpleNamespace
        from voice_runtime import transcribe_command
        transcriber = Mock()
        transcriber.transcribe.return_value = (iter([SimpleNamespace(text=' turn on home mode ')]), None)
        emit, answer = Mock(), Mock()
        transcribe_command(np.zeros(1280), transcriber, emit, answer)
        answer.assert_called_once_with('turn on home mode')
        emit.assert_any_call({'type': 'transcript', 'text': 'turn on home mode'})
