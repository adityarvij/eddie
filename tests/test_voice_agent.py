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
        self.assertEqual('Home mode turned off.', voice_agent.respond('Turn off home mode.'))
        request.assert_called_once_with('/mode', {'home_mode': False})

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure(self, request):
        self.assertIn("couldn't update", voice_agent.respond('turn on home mode'))
        self.assertIn("don't have", voice_agent.respond('give me a summary'))

    @patch('voice_agent.request')
    def test_unsupported_action_does_not_write(self, request):
        self.assertIn("haven't changed", voice_agent.respond('allow netflix'))
        self.assertIn("haven't changed", voice_agent.respond('turn on home mode and send email'))
        request.assert_not_called()

    @patch('voice_agent.request', return_value={'home_mode': False})
    def test_wrong_acknowledgement_is_failure(self, request):
        self.assertIn("couldn't update", voice_agent.respond('turn on home mode'))

class MissedSpeech(unittest.TestCase):
    @patch('voice_agent.request')
    def test_retry_does_not_call_backend(self, request):
        self.assertEqual(voice_agent.respond(''), "Didn't catch that. Try again.")
        request.assert_not_called()

class HomeModeTool(unittest.TestCase):
    @patch('voice_agent.request', return_value={'home_mode': True})
    def test_explicit_enable_phrases(self, request):
        for phrase in ('I want home mode on', 'enable home mode', 'turn home mode on', 'Can you turn on home mode?', 'Please turn on home mode'):
            with self.subTest(phrase=phrase):
                request.reset_mock()
                self.assertEqual('Home mode turned on.', voice_agent.respond(phrase))
                request.assert_called_once_with('/mode', {'home_mode': True})

    @patch('voice_agent.request')
    def test_negations_and_questions_do_not_change_mode(self, request):
        for phrase in ("don't turn on home mode", 'is home mode on', 'I might want home mode on', 'turn on home mode and email Will'):
            voice_agent.respond(phrase)
        request.assert_not_called()

    @patch('voice_agent.request')
    def test_tool_rejects_non_boolean(self, request):
        with self.assertRaises(ValueError):
            voice_agent.set_home_mode('false')
        request.assert_not_called()

class WhatTheyKnowTool(unittest.TestCase):
    @patch('voice_agent.request', return_value={'text': 'Amazon gets the most tracking data.'})
    def test_privacy_questions_read_backend(self, request):
        for phrase in ('What do they know about me?', 'What do companies know about me', 'What does Amazon know about my house?', 'what can the TV tell about us'):
            with self.subTest(phrase=phrase):
                request.reset_mock()
                self.assertEqual('Amazon gets the most tracking data.', voice_agent.respond(phrase))
                request.assert_called_once_with('/what-they-know?hours=168')

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure(self, request):
        self.assertIn("can't say", voice_agent.respond('what do they know about me'))

    @patch('voice_agent.request')
    def test_tool_rejects_bad_window(self, request):
        for hours in (0, 500, '24', True):
            with self.assertRaises(ValueError):
                voice_agent.what_they_know(hours)
        request.assert_not_called()

if __name__ == '__main__':
    unittest.main()
