"""EDDIE_MIC_AUTOSTART: the Pi has no screen, so the dashboard opens the mic itself at start."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve


class Autostart(unittest.TestCase):
    def setUp(self):
        self.runs = 0
        serve.stopped.clear()
        self.addCleanup(self.stop)

    def stop(self):
        serve.stopped.set()
        if serve.voice_thread:
            serve.voice_thread.join(1)
        serve.voice_thread = None

    def microphone(self, fail=0, said_stop=False):
        """A voice loop that dies `fail` times (speakerphone not up yet), then listens until stopped."""
        def run():
            self.runs += 1
            if said_stop:
                serve.stopped.set()
                return
            if self.runs <= fail:
                return
            serve.stopped.wait()
        return run

    def test_listening_on_the_first_try(self):
        with patch.object(serve, 'microphone', self.microphone()):
            self.assertTrue(serve.autostart_microphone(tries=3, wait=0.05))
        self.assertEqual(1, self.runs)
        self.assertTrue(serve.voice_thread.is_alive())

    def test_retries_while_the_speakerphone_comes_up(self):
        with patch.object(serve, 'microphone', self.microphone(fail=2)):
            self.assertTrue(serve.autostart_microphone(tries=3, wait=0.05))
        self.assertEqual(3, self.runs)

    def test_gives_up_after_its_tries(self):
        with patch.object(serve, 'microphone', self.microphone(fail=9)):
            self.assertFalse(serve.autostart_microphone(tries=3, wait=0.05))
        self.assertEqual(3, self.runs)

    def test_stop_recording_is_respected(self):
        with patch.object(serve, 'microphone', self.microphone(said_stop=True)):
            self.assertFalse(serve.autostart_microphone(tries=3, wait=0.05))
        self.assertEqual(1, self.runs)

    def test_start_request_does_not_open_a_second_stream(self):
        with patch.object(serve, 'microphone', self.microphone()):
            self.assertTrue(serve.start_microphone())
            self.assertFalse(serve.start_microphone())
        self.assertEqual(1, self.runs)


if __name__ == '__main__':
    unittest.main()
