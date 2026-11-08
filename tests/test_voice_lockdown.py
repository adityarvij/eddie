"""Voice "lock down <device>" (#17): a read-back and a yes, then keep rules and a device "*" block."""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

TV = 'aa:bb:cc:dd:ee:01'
STICK = 'aa:bb:cc:dd:ee:02'
KEEP = [p for key in voice_agent.LOCKDOWN_KEEP for p in voice_agent.SERVICES[key]['patterns']]
TOTAL = len(KEEP) + 1


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


class FakeBackend:
    """/devices and the /rules contract in memory, with failures on demand."""
    def __init__(self):
        self.devices = [{'mac': TV, 'label': 'TV', 'name': 'TV'},
                        {'mac': STICK, 'label': 'Fire Stick', 'name': 'Fire Stick'},
                        {'mac': '192.168.1.9', 'label': None, 'name': 'Amazon device'}]
        self.rows, self.applied, self.down = {}, True, False
        self.fail_add_at = None  # answer this many POSTs, then stop answering
        self.fail_deletes = False
        self.posts = []

    def __call__(self, path, body=None, timeout=3, method=None):
        if self.down:
            raise OSError('connection refused')
        if path == '/devices':
            return {'hours': 24, 'devices': [dict(d) for d in self.devices]}
        if path == '/rules' and body is not None:
            if self.fail_add_at is not None and len(self.posts) >= self.fail_add_at:
                raise OSError('timed out')
            self.posts.append(body)
            rule_id = max(self.rows, default=0) + 1
            self.rows[rule_id] = {'id': rule_id, 'scope': 'device' if body.get('device_mac') else 'global',
                                  'device_mac': body.get('device_mac'), 'pattern': body['pattern'],
                                  'action': body['action'], 'created_by': body['created_by'], 'ts': 1000 + rule_id}
            return {**self.rows[rule_id], 'applied': self.applied}
        if path == '/rules':
            return [dict(row) for row in sorted(self.rows.values(), key=lambda r: -r['id'])]
        if method == 'DELETE':
            if self.fail_deletes:
                raise OSError('timed out')
            row = self.rows.pop(int(path.rsplit('/', 1)[1]), None)
            if row is None:
                raise urllib.error.HTTPError(path, 404, 'Not Found', {}, io.BytesIO(b'{}'))
            return {**row, 'deleted': True, 'applied': self.applied}
        raise AssertionError(f'unexpected request {method} {path}')


class LockdownBase(unittest.TestCase):
    def setUp(self):
        self.backend, self.clock = FakeBackend(), Clock()
        for name, value in (('request', self.backend), ('undo_stack', []),
                            ('confirmation', voice_agent.Confirmation(clock=self.clock))):
            p = patch.object(voice_agent, name, value)
            p.start()
            self.addCleanup(p.stop)
        voice_agent.forget_conversation()
        self.addCleanup(voice_agent.forget_conversation)

    def ask(self, phrase='Lock down the TV.', name='the TV'):
        reply = voice_agent.respond(phrase)
        self.assertTrue(reply.startswith(f'Lock down {name}? It'), reply)
        return reply

    def rules(self):
        return sorted((r['device_mac'], r['pattern'], r['action']) for r in self.backend.rows.values())


class Phrasing(unittest.TestCase):
    def test_targets(self):
        cases = {
            'Lock down the TV.': ('the ', 'tv'),
            'Eddie, please lock down my Fire Stick': ('my ', 'fire stick'),
            'can you lockdown the kids’ iPad now?': ('the ', 'kids ipad'),
            'lock down the tv and the ipad': None,
            'lock down the tv to netflix': None,
            'lock down the tv except youtube': None,
            "don't lock down the tv": None,
            'how do I lock down the tv': None,
            'block the tv': None,
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.lockdown_target(phrase), expected)

    def test_keep_list_is_every_known_streaming_service(self):
        self.assertEqual(['netflix', 'youtube', 'prime_video', 'apple_tv'], voice_agent.LOCKDOWN_KEEP)


class Confirm(LockdownBase):
    def test_read_back_names_the_device_and_keep_list_and_changes_nothing(self):
        reply = self.ask()
        self.assertEqual(reply, "Lock down the TV? It'll only reach Netflix, YouTube, Prime Video and Apple TV, "
                                "plus Eddie's essentials like sign-in and updates. Say yes to confirm or no to cancel.")
        self.assertEqual([], self.backend.posts)
        self.assertEqual('lockdown', voice_agent.confirmation.status()['kind'])

    def test_yes_writes_keep_rules_then_the_block_last(self):
        self.ask('lock down the fire stick', 'the Fire Stick')
        reply = voice_agent.respond('yes')
        self.assertEqual(reply, "The Fire Stick is locked down. The rules are saved and Eddie's DNS has loaded them. "
                                "I haven't seen its traffic blocked yet, and it only applies while it uses Eddie for DNS.")
        self.assertEqual([{'pattern': p, 'action': 'allow', 'created_by': 'voice', 'device_mac': STICK} for p in KEEP]
                         + [{'pattern': '*', 'action': 'block', 'created_by': 'voice', 'device_mac': STICK}],
                         self.backend.posts)
        self.assertIsNone(voice_agent.confirmation.status())

    def test_not_loaded_is_not_reported_as_blocked(self):
        self.backend.applied = False
        self.ask()
        reply = voice_agent.respond('yes')
        self.assertEqual(reply, "I saved the lockdown rules for the TV, but Eddie's DNS hasn't loaded them yet, "
                                "so nothing is blocked yet.")
        self.assertNotIn('locked down.', reply)

    def test_undo_removes_the_whole_lockdown(self):
        self.ask()
        voice_agent.respond('yes')
        reply = voice_agent.respond('undo')
        self.assertEqual(reply, "Undone. I removed the lockdown rules for the TV, and Eddie's DNS has loaded the change.")
        self.assertEqual([], self.rules())

    def test_already_locked_down(self):
        self.ask()
        voice_agent.respond('yes')
        posts = len(self.backend.posts)
        self.assertEqual(voice_agent.respond('lock down the TV'), 'The TV is already locked down. Nothing changed.')
        self.assertEqual(posts, len(self.backend.posts))
        self.assertIsNone(voice_agent.confirmation.status())


class DeclineAndTimeout(LockdownBase):
    def test_no_changes_nothing(self):
        self.ask()
        self.assertEqual(voice_agent.respond('no'), 'Cancelled. Nothing changed.')
        self.assertIn("don't know how to answer that yet", voice_agent.respond('yes'))
        self.assertEqual([], self.backend.posts)

    def test_timeout_never_executes(self):
        self.ask()
        self.clock.now += voice_agent.CONFIRM_SECONDS
        self.assertEqual(voice_agent.respond('yes'),
                         "That request expired, so I didn't do it. Ask again if you still want it.")
        self.assertEqual([], self.backend.posts)


class Devices(LockdownBase):
    def test_unknown_device_asks_and_changes_nothing(self):
        reply = voice_agent.respond('lock down the fridge')
        self.assertEqual(reply, "I don't know a device called fridge. The devices I know by name are Fire Stick and TV; "
                                "1 more has no name yet and shows as ---. Name it in the dashboard's device list, "
                                "then ask again. Nothing changed.")
        self.assertIsNone(voice_agent.confirmation.status())
        self.assertEqual([], self.backend.posts)

    def test_unlabelled_vendor_names_do_not_count(self):
        self.assertTrue(voice_agent.respond('lock down the amazon device').startswith("I don't know a device"))

    def test_ambiguous_device_asks_and_changes_nothing(self):
        self.backend.devices.append({'mac': 'aa:bb:cc:dd:ee:03', 'label': 'Bedroom TV', 'name': 'Bedroom TV'})
        self.backend.devices[0]['label'] = 'Living Room TV'
        self.assertEqual(voice_agent.respond('lock down the TV'),
                         'Which one: Living Room TV or Bedroom TV? Nothing changed.')
        self.assertIsNone(voice_agent.confirmation.status())
        self.assertEqual([], self.backend.posts)

    def test_exact_label_beats_a_longer_one(self):
        self.backend.devices.append({'mac': 'aa:bb:cc:dd:ee:03', 'label': 'Bedroom TV', 'name': 'Bedroom TV'})
        self.ask()
        voice_agent.respond('yes')
        self.assertEqual({TV}, {body['device_mac'] for body in self.backend.posts})

    def test_one_partial_label_match_is_read_back_in_full(self):
        self.backend.devices[0]['label'] = 'Living Room TV'
        self.assertTrue(voice_agent.respond('lock down the TV').startswith('Lock down the Living Room TV?'))

    def test_backend_down(self):
        self.backend.down = True
        self.assertEqual(voice_agent.respond('lock down the TV'), voice_agent.LOCKDOWN_UNREACHABLE)
        self.assertIsNone(voice_agent.confirmation.status())


class PartialFailure(LockdownBase):
    def test_nothing_saved(self):
        self.backend.fail_add_at = 0
        self.ask()
        self.assertEqual(voice_agent.respond('yes'),
                         "I couldn't lock down the TV; the backend didn't save any rules. Nothing changed.")
        self.assertEqual(voice_agent.respond('undo'), voice_agent.NOTHING_TO_UNDO)

    def test_some_saved_are_rolled_back(self):
        self.backend.fail_add_at = 5
        self.ask()
        self.assertEqual(voice_agent.respond('yes'),
                         f"I only saved 5 of {TOTAL} lockdown rules for the TV before the backend stopped answering, "
                         "so I removed them again. Nothing is locked down.")
        self.assertEqual([], self.rules())
        self.assertEqual(voice_agent.respond('undo'), voice_agent.NOTHING_TO_UNDO)

    def test_block_rule_failing_rolls_back_every_keep_rule(self):
        self.backend.fail_add_at = len(KEEP)
        self.ask()
        self.assertIn('so I removed them again. Nothing is locked down.', voice_agent.respond('yes'))
        self.assertEqual([], self.rules())

    def test_failed_rollback_is_reported_and_left_for_undo(self):
        self.backend.fail_add_at = 3
        self.backend.fail_deletes = True
        self.ask()
        self.assertEqual(voice_agent.respond('yes'),
                         f"I only saved 3 of {TOTAL} lockdown rules for the TV, and couldn't remove 3 of them. "
                         "Nothing is locked down, but those allow rules are still saved; say undo to remove them.")
        self.assertNotIn((TV, '*', 'block'), self.rules())
        self.backend.fail_deletes = False
        self.assertEqual(voice_agent.respond('undo'),
                         "Undone. I removed the leftover lockdown rules for the TV, and Eddie's DNS has loaded the change.")
        self.assertEqual([], self.rules())


if __name__ == '__main__':
    unittest.main()
