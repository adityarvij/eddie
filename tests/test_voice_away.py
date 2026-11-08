"""Voice away mode (#109): on and off wait for a spoken yes; replies only claim what /mode acknowledged."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

SAT = datetime(2026, 11, 7, 15, 30)  # a Saturday afternoon


def ms(dt):
    return int(dt.timestamp() * 1000)


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class FakeBackend:
    """POST /mode {away} as the API answers it."""
    def __init__(self):
        self.applied, self.down, self.posts = True, False, []
        self.devices = [{'key': 'aa:01', 'name': 'TV', 'score': 9}, {'key': 'aa:02', 'name': 'Laptop', 'score': 3}]
        self.exempt = [{'key': 'aa:03', 'name': 'Porch cam'}]

    def __call__(self, path, body=None, timeout=3, method=None):
        if self.down:
            raise OSError('connection refused')
        assert path == '/mode' and body is not None, path
        self.posts.append(body)
        on = body['away']
        return {'away': on, 'away_until': body.get('away_until'), 'away_applied': self.applied,
                'away_devices': self.devices if on else [], 'away_exempt': self.exempt if on else []}


class ParseTests(unittest.TestCase):
    def parse(self, text, now=SAT):
        return voice_agent.away_command(text, now)

    def test_on_phrases_with_and_without_an_end_time(self):
        for text in ('im leaving for the weekend', 'hey eddie were going away', 'turn on away mode',
                     'can you turn on away mode until tuesday please', 'im away for 3 days', 'enable away mode'):
            with self.subTest(text=text):
                kind, args, _ = self.parse(text)
                self.assertEqual(('away', True), (kind, args['on']))
        self.assertEqual({'on': True, 'until': None, 'when': ''}, self.parse('turn on away mode')[1])

    def test_off_phrases(self):
        for text in ('turn off away mode', 'im back', 'were home now', 'disable away mode please'):
            with self.subTest(text=text):
                self.assertEqual(('away', {'on': False}, 'Turn off away mode?'), self.parse(text))

    def test_other_requests_are_not_away_commands(self):
        for text in ('dont turn on away mode', 'is away mode on', 'im leaving the tv on', 'turn on away mode and block it',
                     'im leaving for 0 days', 'turn on home mode'):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_end_times(self):
        cases = {
            'im leaving for the weekend': (datetime(2026, 11, 9), ' until the end of Sunday'),
            'im away until tomorrow': (datetime(2026, 11, 9), ' until the end of tomorrow'),
            'im away until tuesday': (datetime(2026, 11, 11), ' until the end of Tuesday'),
            'im away until saturday': (datetime(2026, 11, 15), ' until the end of Saturday'),  # next week's
            'im away for two hours': (datetime(2026, 11, 7, 17, 30), ' until Saturday 5:30 PM'),
            'im away for a day': (datetime(2026, 11, 8, 15, 30), ' until Sunday 3:30 PM'),
        }
        for text, (end, when) in cases.items():
            with self.subTest(text=text):
                args = self.parse(text)[1]
                self.assertEqual((ms(end), when), (args['until'], args['when']))
        sunday = datetime(2026, 11, 8, 10)
        self.assertEqual(ms(datetime(2026, 11, 9)), self.parse('im leaving for the weekend', sunday)[1]['until'])

    def test_more_than_thirty_days_is_refused(self):
        self.assertEqual(voice_agent.AWAY_TOO_LONG, self.parse('im away for 31 days'))
        self.assertEqual('away', self.parse('im away for 30 days')[0])


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        for p in (patch.object(voice_agent, 'request', self.backend),
                  patch.object(voice_agent, 'confirmation', voice_agent.Confirmation(clock=Clock()))):
            p.start()
            self.addCleanup(p.stop)

    def test_on_waits_for_yes_then_names_covered_and_exempt_devices(self):
        reply = voice_agent.respond("I'm leaving for the weekend.")
        self.assertIn('Turn on away mode until the end of Sunday?', reply)
        self.assertIn('Cameras, doorbells and alarms stay up.', reply)
        self.assertEqual([], self.backend.posts)
        reply = voice_agent.respond('yes')
        self.assertEqual(1, len(self.backend.posts))
        self.assertTrue(self.backend.posts[0]['away'])
        self.assertIsInstance(self.backend.posts[0]['away_until'], int)
        self.assertEqual("Away mode is on until the end of Sunday. Eddie's DNS has loaded it and is cutting "
                         "telemetry from TV and Laptop. Porch cam stays up.", reply)

    def test_no_sends_nothing(self):
        voice_agent.respond('turn on away mode')
        self.assertEqual('Cancelled. Nothing changed.', voice_agent.respond('no'))
        self.assertEqual([], self.backend.posts)

    def test_off_waits_for_yes(self):
        self.assertEqual('Turn off away mode? Say yes to confirm or no to cancel.', voice_agent.respond("I'm back"))
        self.assertEqual("Away mode is off, and Eddie's DNS has loaded the change.", voice_agent.respond('yes'))
        self.assertEqual([{'away': False}], self.backend.posts)

    def test_not_loaded_by_dns_is_said_plainly(self):
        self.backend.applied = False
        voice_agent.respond('turn on away mode')
        self.assertEqual("I saved away mode, but Eddie's DNS hasn't loaded it yet, so nothing is blocked yet.",
                         voice_agent.respond('yes'))
        voice_agent.respond('turn off away mode')
        self.assertIn("hasn't loaded the change yet", voice_agent.respond('yes'))

    def test_no_presence_devices_means_nothing_blocked(self):
        self.backend.devices = []
        voice_agent.respond('turn on away mode')
        self.assertIn("so it isn't blocking anything", voice_agent.respond('yes'))

    def test_many_devices_are_summarised(self):
        self.backend.devices = [{'key': str(i), 'name': f'D{i}', 'score': 1} for i in range(5)]
        self.backend.exempt = []
        voice_agent.respond('turn on away mode')
        self.assertTrue(voice_agent.respond('yes').endswith('cutting telemetry from D0, D1, D2 and 2 more.'))

    def test_unlabelled_addresses_are_counted_not_read_out(self):
        self.backend.devices = [{'key': 'aa:01', 'name': 'TV', 'score': 2},
                                {'key': 'aa:bb:cc:dd:ee:ff', 'name': 'aa:bb:cc:dd:ee:ff', 'score': 1},
                                {'key': '192.168.1.9', 'name': '192.168.1.9', 'score': 1}]
        self.backend.exempt = [{'key': '10.0.0.4', 'name': '10.0.0.4'}]
        self.assertIsNone(voice_agent.ADDRESS.fullmatch('dad'), 'a name made of hex letters is still a name')
        voice_agent.respond('turn on away mode')
        self.assertTrue(voice_agent.respond('yes').endswith(
            'cutting telemetry from TV and 2 unnamed devices. 1 device without a name stays up.'))

    def test_backend_down_or_wrong_echo_is_not_claimed(self):
        self.backend.down = True
        voice_agent.respond('turn on away mode')
        self.assertEqual("I couldn't confirm that went through. Check the local backend before trying again.",
                         voice_agent.respond('yes'))
        self.backend.down = False
        with patch.object(voice_agent, 'request', lambda *a, **k: {'away': False}):
            voice_agent.respond('turn on away mode')
            self.assertIn("couldn't confirm", voice_agent.respond('yes'))

    def test_too_long_is_refused_without_a_prompt(self):
        self.assertEqual(voice_agent.AWAY_TOO_LONG, voice_agent.respond("I'm away for 90 days"))
        self.assertIsNone(voice_agent.confirmation.status())
        self.assertEqual([], self.backend.posts)


if __name__ == '__main__':
    unittest.main()
