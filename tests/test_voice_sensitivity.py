"""Voice sensitivity modes (#110): parsed whole-utterance, read back only what /sensitivity acknowledged."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent


class FakeBackend:
    """POST /sensitivity as the API answers it."""
    def __init__(self):
        self.applied, self.down, self.wrong, self.posts = True, False, False, []

    def __call__(self, path, body=None, timeout=3, method=None):
        if self.down:
            raise OSError('connection refused')
        assert path == '/sensitivity' and body is not None, path
        self.posts.append(body)
        return {'mode': body['mode'], 'on': (not body['on']) if self.wrong else body['on'], 'applied': self.applied,
                'trackers': 74, 'examples': ['BetterHelp', 'CVS', 'GoodRx'], 'sites': 6}


class ParseTests(unittest.TestCase):
    def test_on_phrases(self):
        for text, mode in (('hey eddie protect anything health related', 'health'), ('protect the kids', 'kids'),
                           ('protect anything to do with money', 'money'), ('protect my religious stuff', 'beliefs'),
                           ('turn on health protection please', 'health'), ('enable shopping protection', 'money'),
                           ('can you protect our medical data', 'health'), ('protect political sites', 'beliefs')):
            with self.subTest(text=text):
                self.assertEqual((mode, True), voice_agent.sensitivity_target(text))

    def test_off_phrases(self):
        for text, mode in (('stop protecting health', 'health'), ('turn off kids protection', 'kids'),
                           ('stop protecting anything money related', 'money'), ('disable news protection', 'beliefs')):
            with self.subTest(text=text):
                self.assertEqual((mode, False), voice_agent.sensitivity_target(text))

    def test_other_requests_are_not_sensitivity_commands(self):
        for text in ('dont protect health', 'is health protection on', 'protect the kids tablet',
                     'protect health and block cvscom', 'protect sports', 'turn on home mode', 'protect'):
            with self.subTest(text=text):
                self.assertIsNone(voice_agent.sensitivity_target(text))


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        p = patch.object(voice_agent, 'request', self.backend)
        p.start()
        self.addCleanup(p.stop)

    def test_on_reads_back_what_dns_loaded(self):
        reply = voice_agent.route('Hey Eddie, protect anything health related')
        self.assertEqual([{'mode': 'health', 'on': True}], self.backend.posts)
        self.assertIn("Health protection is on, and Eddie's DNS has loaded it", reply)
        self.assertIn('like BetterHelp, CVS or GoodRx', reply)
        self.assertIn('74 trackers', reply)
        self.assertIn('keep working', reply)

    def test_site_names_fit_the_mode(self):
        self.assertIn("a kids' site", voice_agent.route('protect the kids'))
        self.assertIn('a banking or shopping site', voice_agent.route('protect money'))

    def test_off_reads_back_the_change(self):
        self.assertEqual("Health protection is off, and Eddie's DNS has loaded the change.",
                         voice_agent.route('stop protecting health'))
        self.assertEqual({'mode': 'health', 'on': False}, self.backend.posts[-1])

    def test_not_loaded_never_claims_blocking(self):
        self.backend.applied = False
        reply = voice_agent.route('protect health')
        self.assertIn("hasn't loaded it yet, so nothing is blocked yet", reply)
        self.assertNotIn('I block', reply)
        self.assertIn('may still block', voice_agent.route('stop protecting health'))

    def test_backend_down_or_unacknowledged_changes_nothing_said(self):
        self.backend.down = True
        self.assertEqual("I couldn't change health protection. Check the local backend before trying again.",
                         voice_agent.route('protect health'))
        self.backend.down, self.backend.wrong = False, True
        self.assertIn("couldn't change kids protection", voice_agent.route('protect the kids'))
