"""Privacy settings coach (#112): device → guide lookup, /suggestions and the voice question."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx

from eddie import api, coach, store
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

TV, DOORBELL, LAPTOP, ECHO = 'aa:00:00:00:00:01', 'aa:00:00:00:00:02', 'aa:00:00:00:00:03', 'aa:00:00:00:00:04'


def device(conn, mac, label=None, vendor=None, hostname=None):
    conn.execute("INSERT INTO devices (mac, label, hostname, vendor) VALUES (?, ?, ?, ?)", (mac, label, hostname, vendor))
    conn.commit()


def log(conn, mac, domain, ts):
    conn.execute("INSERT INTO queries (ts, device_mac, domain, qtype, action, source, reason)"
                 " VALUES (?, ?, ?, 'A', 'allow', 'default', '')", (ts, mac, domain))
    conn.commit()


class Lookup(unittest.TestCase):
    def test_names_vendors_and_domains_pick_a_guide(self):
        cases = [
            (('Fire Stick', None, None, set()), 'fire_tv'),
            (('Living room FireTV', None, None, set()), 'fire_tv'),
            ((None, 'Fire-Cube', None, set()), 'fire_tv'),
            (('TV', None, 'Amazon', set()), 'fire_tv'),
            ((None, None, None, {'captive.firetvcaptiveportal.com'}), 'fire_tv'),
            (('Front Door Ring', None, None, set()), 'ring'),
            ((None, None, 'Ring', set()), 'ring'),
            ((None, None, None, {'fw.ring.com'}), 'ring'),
            ((None, None, None, {'ring.com'}), 'ring'),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(expected, coach.kind(*args))

    def test_unrecognised_devices_get_no_guide(self):
        for args in (('Laptop', None, 'Apple', {'apple.com'}),
                     ('Echo', None, 'Amazon', {'device-metrics-us.amazon.com'}),  # Amazon, but not a TV
                     ('Kitchen Speaker', None, None, {'bring.com', 'notring.com'}),  # suffix match is on labels
                     ('Ringo', None, None, set())):
            with self.subTest(args=args):
                self.assertIsNone(coach.kind(*args))

    def test_a_household_name_wins_over_vendor(self):
        self.assertEqual('ring', coach.kind('Ring doorbell', None, 'Amazon', {'firetvcaptiveportal.com'}))

    def test_every_guide_has_steps_and_sources(self):
        for k, guide in coach.GUIDES.items():
            with self.subTest(kind=k):
                self.assertTrue(guide['steps'] and guide['spoken'] and guide['where'])
                self.assertTrue(all(s['url'].startswith('https://') and s['title'] for s in guide['sources']))


class Suggestions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(os.path.join(self.tmp.name, 'eddie.db'))
        self.now = 1_800_000_000_000

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_devices_from_the_table_and_the_window_of_lookups(self):
        device(self.conn, TV, 'Fire Stick', 'Amazon')
        device(self.conn, LAPTOP, 'Laptop', 'Apple')
        log(self.conn, DOORBELL, 'es.ring.com', self.now - 1000)  # never named: recognised by lookups alone
        log(self.conn, ECHO, 'fw.ring.com', self.now - coach.WEEK_MS - 1)  # outside the window
        found = coach.suggestions(self.conn, self.now - coach.WEEK_MS)
        self.assertEqual([{'key': TV, 'name': 'Fire Stick', 'kind': 'fire_tv'},
                          {'key': DOORBELL, 'name': DOORBELL, 'kind': 'ring'}], found)

    def test_spoken_text_groups_devices_and_says_nothing_changed(self):
        text = coach.spoken_text([{'key': TV, 'name': 'Fire Stick', 'kind': 'fire_tv'},
                                  {'key': 'x', 'name': 'Bedroom TV', 'kind': 'fire_tv'},
                                  {'key': DOORBELL, 'name': 'Front Door', 'kind': 'ring'}])
        self.assertTrue(text.startswith('For Fire Stick and Bedroom TV, on the TV open Settings, Preferences, Privacy Settings'))
        self.assertIn('For Front Door, in the Ring app open Control Center', text)
        self.assertIn("I haven't changed any of these myself.", text)

    def test_nothing_recognised_names_the_guides(self):
        self.assertIn('I have privacy guides for Fire TV and Ring', coach.spoken_text([]))


class Endpoint(unittest.IsolatedAsyncioTestCase):
    async def get(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie') as http:
            return await http.get('/suggestions')

    async def test_recognised_device_comes_with_its_guide(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'eddie.db')
            conn = store.connect(path)
            device(conn, TV, 'Fire Stick', 'Amazon')
            log(conn, TV, 'api.amazonvideo.com', int(time.time() * 1000))
            conn.close()
            with patch.dict(os.environ, {'EDDIE_DB': path}):
                r = await self.get()
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual(['Fire TV', 'Ring'], body['guides'])
        [d] = body['devices']
        self.assertEqual(('Fire Stick', 'fire_tv', 'Fire TV'), (d['name'], d['kind'], d['guide']['name']))
        self.assertEqual(coach.GUIDES['fire_tv']['sources'], d['guide']['sources'])
        self.assertTrue(body['text'].startswith('For Fire Stick,'))

    async def test_no_device_log_is_unavailable(self):
        for path in ('off', '/nonexistent/eddie.db'):
            with self.subTest(path=path), patch.dict(os.environ, {'EDDIE_DB': path}):
                r = await self.get()
                self.assertEqual(503, r.status_code)


class Voice(unittest.TestCase):
    @patch('voice_agent.request', return_value={'text': 'For Fire Stick, turn things off.'})
    def test_privacy_questions_read_the_backend_guide(self, request):
        for phrase in ('How can I improve my privacy?', 'how do I improve my privacy', 'What can I do to protect our privacy?',
                       'Any suggestions?', 'Give me some privacy tips', 'How do I make my Fire Stick more private?',
                       'What privacy settings should I change?'):
            with self.subTest(phrase=phrase):
                request.reset_mock()
                self.assertEqual('For Fire Stick, turn things off.', voice_agent.respond(phrase))
                request.assert_called_once_with('/suggestions')

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_failure_changes_nothing(self, request):
        self.assertEqual(voice_agent.UNREACHABLE,
                         voice_agent.respond('how can I improve my privacy'))
        request.assert_called_once_with('/suggestions')


if __name__ == '__main__':
    unittest.main()
