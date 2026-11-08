import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

class VoiceCommands(unittest.TestCase):
    @patch('voice_agent.request', return_value={'text': 'Seven requests were blocked.'})
    def test_summary_uses_backend_facts(self, request):
        self.assertEqual(voice_agent.respond("What's been going on?"), 'Seven requests were blocked.')
        request.assert_called_once_with('/summary')

    @patch('voice_agent.request', return_value={'home_mode': False, 'home_applied': True})
    def test_turn_off(self, request):
        self.assertEqual("Home mode is off and Eddie's DNS has loaded it. It stays off until you change it, "
                         "whoever's phone is home.", voice_agent.respond('Turn off home mode.'))
        request.assert_called_once_with('/mode', {'home_mode': False})

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure(self, request):
        self.assertIn("couldn't update", voice_agent.respond('turn on home mode'))
        self.assertIn("don't have", voice_agent.respond('give me a summary'))

    @patch('voice_agent.request')
    def test_unsupported_action_does_not_write(self, request):
        self.assertIn("haven't changed", voice_agent.respond('lock everything down'))
        self.assertIn("haven't changed", voice_agent.respond('turn on home mode and send email'))
        request.assert_not_called()

    @patch('voice_agent.request', return_value={'home_mode': False})
    def test_wrong_acknowledgement_is_failure(self, request):
        self.assertIn("couldn't update", voice_agent.respond('turn on home mode'))

    @patch('voice_agent.request', return_value={'home_mode': True, 'home_applied': False})
    def test_no_dns_acknowledgement_is_not_claimed(self, request):
        self.assertEqual("Home mode is saved as on, but Eddie's DNS hasn't confirmed it, so I can't say it's being enforced.", voice_agent.respond('turn on home mode'))

class ApiToken(unittest.TestCase):
    """The traffic API takes writes from another host only with EDDIE_API_TOKEN (#96)."""
    def sent_headers(self, env):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        with patch.dict('os.environ', env), patch('urllib.request.urlopen', return_value=response) as urlopen:
            voice_agent.request('/mode', {'home_mode': True})
        return dict(urlopen.call_args[0][0].header_items())

    def test_sends_the_token_when_set(self):
        self.assertEqual(self.sent_headers({'EDDIE_API_TOKEN': 'abc'})['Authorization'], 'Bearer abc')

    def test_sends_none_without_it(self):
        self.assertNotIn('Authorization', self.sent_headers({'EDDIE_API_TOKEN': ''}))

class MissedSpeech(unittest.TestCase):
    @patch('voice_agent.request')
    def test_retry_does_not_call_backend(self, request):
        self.assertEqual(voice_agent.respond(''), "Didn't catch that. Try again.")
        request.assert_not_called()


class NoModelFallback(unittest.TestCase):
    def setUp(self):
        self.model = patch.object(voice_agent, 'ask_model', None)
        self.model.start()
        self.addCleanup(self.model.stop)

    @patch('voice_agent.request')
    def test_unmatched_question_points_to_capabilities_without_a_write(self, request):
        reply = voice_agent.respond('Can you recommend a movie for tonight?')
        self.assertEqual("I don't know how to answer that yet. Tap capabilities, bottom right, to see what I can do.", reply)
        request.assert_not_called()

    @patch('voice_agent.request')
    def test_unparseable_command_changes_nothing(self, request):
        reply = voice_agent.respond('Block some bad stuff and send an email')
        self.assertIn("haven't changed anything", reply)
        request.assert_not_called()

class HomeModeTool(unittest.TestCase):
    @patch('voice_agent.request', return_value={'home_mode': True})
    def test_explicit_enable_phrases(self, request):
        for phrase in ('I want home mode on', 'enable home mode', 'turn home mode on', 'Can you turn on home mode?', 'Please turn on home mode',
                       'Itty, turn on home mode please.', 'Home mode on.', 'Hey Eddie, turn on the home mode', 'Eddie activate home mode now'):
            with self.subTest(phrase=phrase):
                request.reset_mock()
                self.assertTrue(voice_agent.respond(phrase).startswith('Home mode is saved as on'))
                request.assert_called_once_with('/mode', {'home_mode': True})

    @patch('voice_agent.request')
    def test_negations_and_questions_do_not_change_mode(self, request):
        for phrase in ("don't turn on home mode", 'is home mode on', 'I might want home mode on', 'turn on home mode and email Will',
                       'what is home mode on', 'do not turn on home mode', 'should I turn on home mode', 'maybe home mode on'):
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
                request.assert_called_once_with('/what-they-know')

    @patch('voice_agent.request', return_value={'facts': [], 'enough_data': False, 'text': 'Not enough data.'})
    def test_privacy_question_uses_requested_window_and_can_decline(self, request):
        for phrase, hours in (('What do they know about me in the last 3 days?', 72),
                              ('What could companies guess about us over two weeks?', 336),
                              ('What does Amazon know about my house this month?', 720)):
            with self.subTest(phrase=phrase):
                request.reset_mock()
                self.assertEqual('Not enough data.', voice_agent.respond(phrase))
                request.assert_called_once_with(f'/what-they-know?hours={hours}')

    def test_report_window_parser_defaults_to_all_retained_data(self):
        self.assertIsNone(voice_agent.report_hours('what do they know about me'))
        self.assertEqual(24, voice_agent.report_hours('what do they know about me today'))
        self.assertEqual(1, voice_agent.report_hours('what do they know from the last 20 minutes'))

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure(self, request):
        self.assertIn("can't say", voice_agent.respond('what do they know about me'))

    @patch('voice_agent.request')
    def test_tool_rejects_bad_window(self, request):
        for hours in (0, 9000, '24', True):
            with self.assertRaises(ValueError):
                voice_agent.what_they_know(hours)
        request.assert_not_called()

class BurglarsEyeTool(unittest.TestCase):
    @patch('voice_agent.request', return_value={'text': "From this week's data, someone could tell the house was empty."})
    def test_empty_house_questions_read_backend_with_the_spoken_range(self, request):
        for phrase, path in (('When could someone tell the house was empty?', '/burglars-eye'),
                             ('When does my house look empty?', '/burglars-eye'),
                             ("Give me the burglar's eye report", '/burglars-eye'),
                             ('When was nobody home in the last 3 days?', '/burglars-eye?hours=72'),
                             ('Could a burglar tell my place was empty today?', '/burglars-eye?hours=24'),
                             ('when is the home empty over the past 2 weeks', '/burglars-eye?hours=336')):
            with self.subTest(phrase=phrase):
                request.reset_mock()
                self.assertIn('someone could tell the house was empty', voice_agent.respond(phrase))
                request.assert_called_once_with(path)

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure(self, request):
        self.assertEqual("I can't reach Eddie’s traffic backend, so I can't say when the house looked empty right now.",
                         voice_agent.respond('when could someone tell the house was empty'))

    @patch('voice_agent.request')
    def test_tool_rejects_bad_window(self, request):
        for hours in (0, 9000, '24', True):
            with self.assertRaises(ValueError):
                voice_agent.burglars_eye(hours)
        request.assert_not_called()

class HomeModeOff(unittest.TestCase):
    def test_off_phrasings(self):
        for phrase in ('turn off home mode', 'Home mode off.', 'Itty, turn off home mode please', 'disable home mode',
                       'Hey Eddie, switch the home mode off'):
            with self.subTest(phrase=phrase):
                self.assertIs(False, voice_agent.home_mode_command(voice_agent.words(phrase)))
        for phrase in ("don't turn off home mode", 'is home mode off', 'turn off home mode and block it'):
            with self.subTest(phrase=phrase):
                self.assertIsNone(voice_agent.home_mode_command(voice_agent.words(phrase)))


if __name__ == '__main__':
    unittest.main()
