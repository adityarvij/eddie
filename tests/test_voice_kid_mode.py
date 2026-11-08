"""Kid mode (#119): children's devices, marked on the dashboard, locked down to the kid-safe list."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent
from tests.test_voice_lockdown import STICK, TV, LockdownBase

TABLET = 'aa:bb:cc:dd:ee:04'
UNNAMED = 'aa:bb:cc:dd:ee:05'
KID = [p for key in voice_agent.KID_KEEP for p in voice_agent.SERVICES[key]['patterns']]


class Phrasing(unittest.TestCase):
    def test_on_and_off(self):
        cases = {
            'Turn on kid mode.': True, 'turn kids mode on please': True, 'Eddie, enable kid mode': True,
            'kid mode on': True, "Lock down the kids' devices": True, 'lock down all the children’s devices': True,
            'turn off kid mode': False, 'switch kid mode off': False, 'disable kids mode': False,
            "unlock the kids' devices": False,
            "lock down the kids' tablet": None, "don't turn on kid mode": None, 'what is kid mode': None,
            'turn on kid mode and block netflix.com': None,
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.kid_mode_target(phrase), expected)

    def test_kid_keep_list_is_seeded_and_kept_out_of_plain_lockdown(self):
        self.assertEqual(['youtube_kids', 'pbs_kids'], voice_agent.KID_KEEP)
        self.assertFalse(set(voice_agent.KID_KEEP) & set(voice_agent.LOCKDOWN_KEEP))


class KidBase(LockdownBase):
    def setUp(self):
        super().setUp()
        self.backend.devices += [
            {'mac': TABLET, 'label': "Kids' Tablet", 'name': "Kids' Tablet", 'kid': True},
            {'mac': UNNAMED, 'label': None, 'vendor': 'Amazon', 'name': 'Amazon device', 'kid': True}]
        self.deleted = []
        backend = self.backend

        def recording(path, body=None, timeout=3, method=None):
            if method == 'DELETE':
                self.deleted.append(backend.rows.get(int(path.rsplit('/', 1)[1]), {}).get('pattern'))
            return backend(path, body, timeout, method)
        p = patch.object(voice_agent, 'request', recording)
        p.start()
        self.addCleanup(p.stop)

    def device_rules(self, mac):
        return sorted((r['pattern'], r['action'], r['created_by']) for r in self.backend.rows.values()
                      if r['device_mac'] == mac)


class LockDownByName(KidBase):
    def test_a_childs_device_keeps_the_kid_safe_list(self):
        reply = voice_agent.respond("lock down the kids' tablet")
        self.assertEqual(reply, "Lock down the Kids' Tablet? It's marked as a child's, so it'll only reach YouTube Kids "
                                "and PBS Kids, plus Eddie's essentials like sign-in and updates. "
                                "Say yes to confirm or no to cancel.")
        voice_agent.respond('yes')
        self.assertEqual(sorted([(p, 'allow', 'voice') for p in KID] + [('*', 'block', 'voice')]),
                         self.device_rules(TABLET))

    def test_an_unnamed_device_cant_be_named_by_its_placeholder(self):
        for phrase in ('lock down add a title', 'lock down ---', 'lock down the amazon device'):
            with self.subTest(phrase=phrase):
                reply = voice_agent.respond(phrase)
                self.assertFalse(reply.startswith('Lock down'), reply)
                self.assertIsNone(voice_agent.confirmation.status())
        self.assertEqual([], self.backend.posts)

    def test_unknown_device_counts_the_unnamed_as_shown(self):
        reply = voice_agent.respond('lock down the fridge')
        self.assertIn("The devices I know by name are Fire Stick, Kids' Tablet and TV; 2 more have no name yet "
                      "and show as ---.", reply)
        self.backend.devices = [d for d in self.backend.devices if not d['label']]
        self.assertIn("None of your devices has a name yet; they show as ---.", voice_agent.respond('lock down the fridge'))


class TurnOn(KidBase):
    def test_read_back_names_every_childs_device_and_changes_nothing(self):
        reply = voice_agent.respond('turn on kid mode')
        self.assertEqual(reply, "Turn on kid mode? Kids' Tablet and an unnamed Amazon device will only reach YouTube Kids "
                                "and PBS Kids, plus Eddie's essentials like sign-in and updates. "
                                "Say yes to confirm or no to cancel.")
        self.assertEqual('kid_mode', voice_agent.confirmation.status()['kind'])
        self.assertEqual([], self.backend.posts)
        self.assertEqual('Cancelled. Nothing changed.', voice_agent.respond('no'))
        self.assertEqual([], self.backend.posts)

    def test_yes_locks_down_only_the_childrens_devices(self):
        voice_agent.respond('turn on kid mode')
        reply = voice_agent.respond('yes')
        self.assertEqual(reply, "Kid mode is on. Kids' Tablet and an unnamed Amazon device are locked down to YouTube "
                                "Kids and PBS Kids, plus Eddie's essentials; Eddie's DNS has loaded the rules. "
                                "I haven't seen their traffic blocked yet, and it only applies while they use Eddie for DNS.")
        expected = sorted([(p, 'allow', 'voice') for p in KID] + [('*', 'block', 'voice')])
        self.assertEqual(expected, self.device_rules(TABLET))
        self.assertEqual(expected, self.device_rules(UNNAMED))
        self.assertEqual([], self.device_rules(TV) + self.device_rules(STICK))
        self.assertEqual("Kid mode is already on for Kids' Tablet and an unnamed Amazon device. Nothing changed.",
                         voice_agent.respond('turn on kid mode'))

    def test_only_the_devices_not_yet_locked_are_asked_about(self):
        voice_agent.respond("lock down the kids' tablet")
        voice_agent.respond('yes')
        self.assertTrue(voice_agent.respond('kid mode on').startswith('Turn on kid mode? An unnamed Amazon device will'))

    def test_no_childs_device_yet(self):
        for device in self.backend.devices:
            device['kid'] = False
        self.assertEqual(voice_agent.respond('turn on kid mode'),
                         "No device is marked as a child's yet. Open devices on the dashboard and set one to child, "
                         "then ask again. Nothing changed.")
        self.assertIsNone(voice_agent.confirmation.status())

    def test_not_loaded_is_not_reported_as_on(self):
        self.backend.applied = False
        voice_agent.respond('turn on kid mode')
        reply = voice_agent.respond('yes')
        self.assertNotIn('Kid mode is on', reply)
        self.assertIn("I saved the lockdown rules for Kids' Tablet, but Eddie's DNS hasn't loaded them yet", reply)

    def test_a_failure_part_way_says_what_happened(self):
        voice_agent.respond('turn on kid mode')
        self.backend.fail_add_at = len(KID) + 1  # the tablet's rules all land, then the backend stops answering
        reply = voice_agent.respond('yes')
        self.assertTrue(reply.startswith("Kids' Tablet is locked down to YouTube Kids and PBS Kids"), reply)
        self.assertIn("I couldn't lock down an unnamed Amazon device; the backend didn't save any rules.", reply)
        self.assertEqual([], self.device_rules(UNNAMED))

    def test_undo_removes_what_voice_added(self):
        voice_agent.respond('turn on kid mode')
        voice_agent.respond('yes')
        voice_agent.respond('undo')
        voice_agent.respond('undo')
        self.assertEqual([], self.rules())

    def test_unreachable_backend_changes_nothing(self):
        self.backend.down = True
        self.assertEqual(voice_agent.KID_UNREACHABLE, voice_agent.respond('turn on kid mode'))


class TurnOff(KidBase):
    def setUp(self):
        super().setUp()
        voice_agent.respond('turn on kid mode')
        voice_agent.respond('yes')
        voice_agent.add_rule('ads.example', 'block', device=TABLET)  # not kid mode's: stays

    def test_off_removes_the_block_first_and_keeps_other_rules(self):
        reply = voice_agent.respond('turn off kid mode')
        self.assertEqual(reply, "Turn off kid mode? Kids' Tablet and an unnamed Amazon device will get full internet "
                                "through Eddie again. Say yes to confirm or no to cancel.")
        self.assertEqual([], self.deleted)
        reply = voice_agent.respond('yes')
        self.assertEqual(reply, "Kid mode is off. Kids' Tablet and an unnamed Amazon device have full internet through "
                                "Eddie again; Eddie's DNS has loaded the change.")
        self.assertEqual('*', self.deleted[0])
        self.assertEqual([('ads.example', 'block', 'voice')], self.device_rules(TABLET))
        self.assertEqual([], self.device_rules(UNNAMED))
        self.assertEqual("Kid mode is already off for Kids' Tablet and an unnamed Amazon device. Nothing changed.",
                         voice_agent.respond('turn off kid mode'))

    def test_a_failed_delete_says_it_is_still_locked(self):
        voice_agent.respond('turn off kid mode')
        self.backend.fail_deletes = True
        reply = voice_agent.respond('yes')
        self.assertIn("I couldn't unlock Kids' Tablet; the backend didn't confirm it. It's still locked down.", reply)
        self.assertNotIn('Kid mode is off', reply)


class Dashboard(KidBase):
    def test_the_dashboard_switch_writes_dashboard_rules_voice_cant_undo(self):
        reply = voice_agent.kid_mode_change(True)
        self.assertTrue(reply.startswith('Kid mode is on.'), reply)
        self.assertEqual(sorted([(p, 'allow', 'dashboard') for p in KID] + [('*', 'block', 'dashboard')]),
                         self.device_rules(TABLET))
        self.assertEqual([], voice_agent.undo_stack)
        self.assertIsNone(voice_agent.confirmation.status(), 'the page asked already; nothing waits for a spoken yes')
        self.assertTrue(voice_agent.kid_mode_change(False).startswith('Kid mode is off.'))
        self.assertEqual([], self.device_rules(TABLET))

    def test_nothing_to_change_is_said_plainly(self):
        self.assertEqual("Kid mode is already off for Kids' Tablet and an unnamed Amazon device. Nothing changed.",
                         voice_agent.kid_mode_change(False))


if __name__ == '__main__':
    unittest.main()
