"""Voice "allow the new device" / "deny the new device" (#98): a read-back and a yes, then the backend's ack."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

OLD = 'aa:bb:cc:dd:ee:01'
NEW = 'aa:bb:cc:dd:ee:02'
NEWEST = 'aa:bb:cc:dd:ee:03'
TWO_FOURTEEN = int(time.mktime((2026, 11, 7, 2, 14, 0, 0, 0, -1)) * 1000)


class FakeBackend:
    """/devices and POST /devices/<mac>/approve|deny in memory."""
    def __init__(self):
        self.devices = [{'mac': OLD, 'name': 'TV', 'status': 'approved', 'first_seen': 1},
                        {'mac': NEW, 'name': 'Amazon device', 'status': 'pending', 'first_seen': TWO_FOURTEEN}]
        self.applied, self.down, self.posts = True, False, []

    def __call__(self, path, body=None, timeout=3, method=None):
        if self.down:
            raise OSError('connection refused')
        if path == '/devices':
            return {'hours': 24, 'devices': [dict(d) for d in self.devices]}
        mac, decision = path.split('/')[2:4]
        self.posts.append(path)
        status = 'approved' if decision == 'approve' else 'denied'
        return {'mac': mac, 'name': 'Amazon device', 'status': status, 'applied': self.applied}


class NewDeviceVoiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        for p in (patch.object(voice_agent, 'request', self.backend),
                  patch.object(voice_agent, 'confirmation', voice_agent.Confirmation())):
            p.start()
            self.addCleanup(p.stop)

    def test_read_back_names_the_device_and_time_and_changes_nothing(self):
        reply = voice_agent.route('Allow the new device')
        self.assertEqual("Allow the new device, Amazon device? Yes or no?", reply)
        self.assertEqual([], self.backend.posts)

    def test_yes_approves_and_reports_the_dns_ack(self):
        voice_agent.route('approve the new device')
        self.assertEqual("Amazon device approved.", voice_agent.route('yes'))
        self.assertEqual([f'/devices/{NEW}/approve'], self.backend.posts)

    def test_not_loaded_is_not_reported_as_done(self):
        self.backend.applied = False
        voice_agent.route('allow the new device')
        self.assertEqual(voice_agent.SAVED_NOT_LOADED,
                         voice_agent.route('yes'))

    def test_deny_says_it_outlasts_quarantine(self):
        reply = voice_agent.route('block the new device')
        self.assertEqual('Deny the new device, Amazon device? Yes or no?', reply)
        self.assertEqual('Amazon device denied.', voice_agent.route('yes'))
        self.assertEqual([f'/devices/{NEW}/deny'], self.backend.posts)

    def test_no_changes_nothing(self):
        voice_agent.route('allow the new device')
        self.assertEqual('Cancelled.', voice_agent.route('no'))
        self.assertEqual([], self.backend.posts)

    def test_newest_pending_device_first_and_others_mentioned(self):
        self.backend.devices.append({'mac': NEWEST, 'name': 'kindle', 'status': 'pending',
                                     'first_seen': TWO_FOURTEEN + 60_000})
        reply = voice_agent.route('allow the new device')
        self.assertIn('Allow the new device, kindle?', reply)

    def test_nothing_pending(self):
        self.backend.devices = self.backend.devices[:1]
        self.assertEqual("There's no new device waiting.",
                         voice_agent.route('allow the new device'))

    def test_backend_down(self):
        self.backend.down = True
        self.assertEqual(voice_agent.UNREACHABLE, voice_agent.route('allow the new device'))

    def test_other_allows_still_route(self):
        self.assertEqual(('domain', 'news.example'), voice_agent.allow_target('allow news.example'))
        self.assertIsNone(voice_agent.NEW_DEVICE.fullmatch(voice_agent.words('allow the new device and the tv')))


if __name__ == '__main__':
    unittest.main()
