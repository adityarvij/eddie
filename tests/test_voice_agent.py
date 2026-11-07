import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

class VoiceCommands(unittest.TestCase):
    @patch('voice_agent.request', return_value={'text': 'Seven requests were blocked.'})
    def test_summary_uses_backend_facts(self, request):
        self.assertEqual(voice_agent.respond("What's been going on?"), 'Seven requests were blocked.')
        request.assert_called_once_with('/summary')

    @patch('voice_agent.request', return_value={'home_mode': False})
    def test_turn_off(self, request):
        self.assertIn('off in', voice_agent.respond('Turn off home mode.'))
        request.assert_called_once_with('/mode', {'home_mode': False})

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure(self, request):
        self.assertIn("couldn't update", voice_agent.respond('turn on home mode'))
        self.assertIn("don't have", voice_agent.respond('give me a summary'))

    @patch('voice_agent.request')
    def test_unsupported_action_does_not_write(self, request):
        self.assertIn("haven't changed", voice_agent.respond('block netflix'))
        self.assertIn("haven't changed", voice_agent.respond('turn on home mode and send email'))
        request.assert_not_called()

    @patch('voice_agent.request', return_value={'home_mode': False})
    def test_wrong_acknowledgement_is_failure(self, request):
        self.assertIn("couldn't update", voice_agent.respond('turn on home mode'))

if __name__ == '__main__':
    unittest.main()
