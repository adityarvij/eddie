"""Turning the microphone off by voice (#128): "stop recording" and the local model's stop_microphone."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve
import ollama_agent
import voice_agent

ON = 'Microphone turned off.'


class StopRecording(unittest.TestCase):
    def setUp(self):
        self.saved = voice_agent.microphone_off
        voice_agent.microphone_off = Mock(return_value=True)

    def tearDown(self):
        voice_agent.microphone_off = self.saved
        voice_agent.ask_model = None
        voice_agent.confirmation.cancel()

    def test_stop_phrases_turn_the_mic_off(self):
        for said in ('Stop recording.', 'stop listening', 'Hey Eddie, stop recording', 'okay stop listening please',
                     'turn off the mic', 'Turn the microphone off.', 'could you stop recording', 'mic off',
                     'shut down the microphone', 'stop the mic'):
            voice_agent.microphone_off.reset_mock()
            self.assertEqual(voice_agent.respond(said), ON, said)
            voice_agent.microphone_off.assert_called_once_with()

    def test_other_requests_leave_the_mic_on(self):
        voice_agent.ask_model = lambda text: 'From the model.'
        for said in ("don't stop recording", 'stop recording the TV', 'stop', 'is the mic off', 'turn off home mode'):
            with patch('voice_agent.request', return_value={'home_mode': False}):
                voice_agent.respond(said)
        voice_agent.microphone_off.assert_not_called()

    def test_already_off_says_so(self):
        voice_agent.microphone_off.return_value = False
        self.assertEqual(voice_agent.respond('stop recording'), 'The microphone is already off.')
        voice_agent.microphone_off = None
        self.assertEqual(voice_agent.respond('stop recording'), 'The microphone is already off.')

    def test_stop_needs_no_yes_and_beats_a_pending_reply(self):
        voice_agent.confirmation.request('home_mode', {'enabled': True}, 'Turn home mode on?')
        self.assertEqual(voice_agent.respond('stop recording'), ON)
        voice_agent.microphone_off.assert_called_once_with()

    def test_model_tool_turns_the_mic_off(self):
        self.assertIn(voice_agent.STOP_MIC_TOOL, ollama_agent.TOOLS)
        model = Mock(return_value={'content': '', 'tool_calls': [{'function': {'name': 'stop_microphone', 'arguments': {}}}]})
        agent = ollama_agent.Agent(chat=model)
        self.assertEqual(agent.ask('please quit listening to us'), ON)
        voice_agent.microphone_off.assert_called_once_with()
        self.assertEqual(model.call_count, 1)


class BridgeHook(unittest.TestCase):
    def tearDown(self):
        serve.stopped.clear()
        serve.voice_thread = None

    def test_serve_wires_the_hook(self):
        self.assertIs(voice_agent.microphone_off, serve.microphone_off)

    def test_stops_a_running_mic_once(self):
        release = threading.Event()
        serve.voice_thread = threading.Thread(target=release.wait, daemon=True)
        serve.voice_thread.start()
        try:
            self.assertTrue(serve.microphone_off())
            self.assertTrue(serve.stopped.is_set())
            self.assertFalse(serve.microphone_off(), 'already stopping')
        finally:
            release.set()

    def test_no_mic_thread_means_already_off(self):
        serve.voice_thread = None
        self.assertFalse(serve.microphone_off())


if __name__ == '__main__':
    unittest.main()
