"""Guest Mode permissions (#163): deny by default, owner-only tools refused, approved ones allowed."""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve
import guest
import ollama_agent
import timers
import voice_agent

OWNER_ONLY = guest.OWNER_ONLY_REPLY
NOT_APPROVED = guest.NOT_APPROVED_REPLY


class Profile(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'guest.json'

    def test_off_and_nothing_allowed_by_default(self):
        profile = guest.Profile(self.path)
        self.assertEqual(('owner', set()), (profile.role(), profile.allowed))
        self.assertFalse(self.path.exists(), 'nothing is written until the owner saves')

    def test_guest_gets_only_what_was_approved(self):
        profile = guest.Profile()
        profile.update(active=True)
        for category in guest.APPROVABLE + guest.OWNER_ONLY + ('unknown',):
            self.assertFalse(profile.allows(category), category)
        for category in guest.ALWAYS:
            self.assertTrue(profile.allows(category), category)
        profile.update(allowed=['timers', 'tv'])
        self.assertEqual({'timers', 'tv'}, {c for c in guest.APPROVABLE if profile.allows(c)})
        self.assertEqual('guest', profile.role())

    def test_owner_has_every_known_category(self):
        profile = guest.Profile()
        for category in guest.APPROVABLE + guest.OWNER_ONLY + guest.ALWAYS:
            self.assertIsNone(profile.check(category), category)
        self.assertFalse(profile.allows('unknown'), 'an unmapped tool is denied even to the owner')

    def test_owner_only_categories_cant_be_approved(self):
        profile = guest.Profile(self.path)
        for allowed in (['traffic'], ['contacts', 'timers'], ['calendars'], 'timers', [1]):
            with self.assertRaises(ValueError):
                profile.update(allowed=allowed)
        with self.assertRaises(ValueError):
            profile.update(active='yes')
        self.assertEqual(('owner', set()), (profile.role(), profile.allowed))

    def test_saved_locally_owner_only_and_reloaded(self):
        guest.Profile(self.path).update(active=True, allowed=['tv', 'timers'])
        self.assertEqual({'active': True, 'allowed': ['timers', 'tv']}, json.loads(self.path.read_text()))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(self.path).st_mode))
        reloaded = guest.Profile(self.path)
        self.assertEqual(('guest', {'tv', 'timers'}), (reloaded.role(), reloaded.allowed))

    def test_a_hand_edited_owner_only_category_is_dropped(self):
        self.path.write_text(json.dumps({'active': True, 'allowed': ['traffic', 'contacts', 'lights']}))
        self.assertEqual({'lights'}, guest.Profile(self.path).allowed)

    def test_unreadable_file_fails_closed(self):
        for bad in ('not json', '[]', '{"active": "no", "allowed": []}', '{"active": false}'):
            self.path.write_text(bad)
            profile = guest.Profile(self.path)
            self.assertEqual(('guest', set()), (profile.role(), profile.allowed), bad)
            self.assertIn('unreadable', profile.load_error)
        profile.update(active=False)
        self.assertIsNone(profile.load_error)
        self.assertEqual('owner', guest.Profile(self.path).role())

    def test_failed_save_changes_nothing(self):
        profile = guest.Profile(self.path / 'missing-folder' / 'guest.json')
        with self.assertRaises(OSError):
            profile.update(active=True, allowed=['tv'])
        self.assertEqual(('owner', set()), (profile.role(), profile.allowed))


class GuestRouting(unittest.TestCase):
    """Every router category with Guest Mode on. Refusals happen before any backend call."""

    def setUp(self):
        self.saved = (voice_agent.profile, voice_agent.timer_store, voice_agent.guest_timers,
                      voice_agent.microphone_off, voice_agent.ask_model, voice_agent.text_sender)
        voice_agent.profile = guest.Profile()
        voice_agent.profile.update(active=True)
        voice_agent.timer_store = timers.Timers()
        voice_agent.guest_timers = timers.Timers()
        voice_agent.microphone_off = Mock(return_value=True)
        voice_agent.ask_model = Mock(return_value='From the model.')
        voice_agent.text_sender = Mock()
        self.request = Mock(side_effect=AssertionError('no backend call for a guest'))
        patcher = patch.object(voice_agent, 'request', self.request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        (voice_agent.profile, voice_agent.timer_store, voice_agent.guest_timers,
         voice_agent.microphone_off, voice_agent.ask_model, voice_agent.text_sender) = self.saved
        voice_agent.confirmation.cancel()
        voice_agent.forget_conversation()

    def test_owner_only_tools_are_refused(self):
        phrases = {
            'traffic': ["What's been going on?", "what's the TV doing", 'give me a summary'],
            'reports': ['What does Amazon know about me?', 'When did the house look empty?'],
            'devices': ['approve the new device', 'deny that new device', 'any suggestions'],
            'rules': ['block tiktok.com', 'allow example.com', 'allow netflix', 'allow *.example.com',
                      'lock down the TV to Netflix', 'undo that', 'block the TV from talking to Amazon'],
            'modes': ['turn on home mode', 'turn off home mode', 'turn on kid mode', 'turn on away mode',
                      "I'm leaving for the weekend", 'turn off guest mode', 'turn on guest mode'],
        }
        for category, said in phrases.items():
            for text in said:
                self.assertEqual(OWNER_ONLY, voice_agent.respond(text), f'{category}: {text}')
        self.request.assert_not_called()
        voice_agent.ask_model.assert_not_called()
        self.assertIsNone(voice_agent.confirmation.status())

    def test_contacts_are_refused_without_naming_anyone(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        contacts = Path(folder.name) / 'contacts.json'
        contacts.write_text(json.dumps({'Sam': '+15557654321'}))
        with patch.dict(os.environ, {'EDDIE_CONTACTS': str(contacts)}):
            for text in ("text Sam I'm running late", 'text Bob hello', 'text Sam'):
                reply = voice_agent.respond(text)
                self.assertEqual(OWNER_ONLY, reply, text)
        voice_agent.text_sender.send.assert_not_called()

    def test_calendars_and_anything_else_get_the_guest_help(self):
        for text in ("What's on my calendar today?", 'who lives here'):
            self.assertEqual(guest.HELP, voice_agent.respond(text), text)
        voice_agent.ask_model.assert_not_called()

    def test_greeting_and_stop_recording_are_always_allowed(self):
        self.assertEqual(f"Hey, I'm Eddie. {guest.HELP}", voice_agent.respond('hello'))
        self.assertTrue(voice_agent.respond('stop recording').startswith("Okay, I'm turning the microphone off"))
        voice_agent.microphone_off.assert_called_once_with()

    def test_timers_need_the_owners_approval(self):
        for text in ('set a timer for 10 minutes', 'what timers do i have', 'remind me in 5 minutes to check the oven'):
            self.assertEqual(NOT_APPROVED, voice_agent.respond(text), text)
        self.assertEqual([], voice_agent.guest_timers.pending() + voice_agent.timer_store.pending())

    def test_approved_timers_are_the_guests_own(self):
        now = voice_agent.timer_store.clock()
        voice_agent.timer_store.add('reminder', now + 3600, task='call my mom')
        voice_agent.profile.update(allowed=['timers'])
        self.assertEqual("You don't have any timers or reminders.", voice_agent.respond('what timers do i have'))
        self.assertEqual("You don't have any reminders.", voice_agent.respond('cancel my reminders'))
        self.assertEqual('Timer set for 10 minutes.', voice_agent.respond('set a timer for 10 minutes'))
        self.assertNotIn('mom', voice_agent.respond('list my timers and reminders'))
        self.assertEqual(1, len(voice_agent.guest_timers.pending()))
        self.assertEqual(['call my mom'], [item['task'] for item in voice_agent.timer_store.pending()])

    def test_a_guests_yes_never_runs_the_owners_pending_change(self):
        voice_agent.profile.update(active=False)
        voice_agent.confirmation.request('home_mode', {'enabled': True}, 'Turn home mode on?')
        voice_agent.profile.update(active=True)
        self.assertEqual(OWNER_ONLY, voice_agent.respond('yes'))
        self.assertIsNone(voice_agent.confirmation.status())
        self.request.assert_not_called()

    def test_every_risky_action_is_refused_at_confirmation(self):
        for kind in voice_agent.RISKY_ACTIONS:
            self.assertEqual(OWNER_ONLY, voice_agent.confirmation.request(kind, {}, 'Do it?'), kind)
        self.assertEqual(set(voice_agent.RISKY_ACTIONS), set(voice_agent.RISKY_CATEGORIES))

    def test_owner_mode_is_unchanged(self):
        voice_agent.profile.update(active=False)
        self.request.side_effect = None
        self.request.return_value = {'text': 'Quiet hour.'}
        voice_agent.ask_model = None
        self.assertEqual('Quiet hour.', voice_agent.respond("What's been going on?"))
        self.assertEqual('Timer set for 10 minutes.', voice_agent.respond('set a timer for 10 minutes'))
        self.assertEqual(1, len(voice_agent.timer_store.pending()))
        self.assertTrue(voice_agent.respond('turn on away mode').startswith('Turn on away mode?'))


class GuestModelTools(unittest.TestCase):
    """The local model's tools check the role too, in case it's ever reached in Guest Mode."""

    def setUp(self):
        self.saved = voice_agent.profile, voice_agent.microphone_off
        voice_agent.profile = guest.Profile()
        voice_agent.profile.update(active=True)
        voice_agent.microphone_off = Mock(return_value=True)

    def tearDown(self):
        voice_agent.profile, voice_agent.microphone_off = self.saved
        voice_agent.confirmation.cancel()

    def test_every_read_tool_is_refused(self):
        self.assertEqual(set(ollama_agent.HANDLERS) | {'stop_microphone'}, set(ollama_agent.TOOL_CATEGORIES))
        agent = ollama_agent.Agent(chat=Mock())
        with patch.object(ollama_agent, 'read', side_effect=AssertionError('no backend read')):
            for name in ollama_agent.HANDLERS:
                call = {'function': {'name': name, 'arguments': {}}}
                self.assertEqual((name, {'error': OWNER_ONLY}), agent.run_tool(call), name)

    def test_write_tools_are_refused(self):
        for name, args in (('block', {'domain': 'tiktok.com'}), ('allow', {'service': 'netflix'}),
                           ('set_mode', {'home_mode': True})):
            model = Mock(return_value={'content': '', 'tool_calls': [{'function': {'name': name, 'arguments': args}}]})
            self.assertEqual(OWNER_ONLY, ollama_agent.Agent(chat=model).ask('do it'), name)
        self.assertIsNone(voice_agent.confirmation.status())

    def test_local_intents_are_refused(self):
        try:
            import local_intent  # needs numpy (the voice venv)
        except ImportError:
            self.skipTest('needs numpy')
        matcher = Mock()
        matcher.match.return_value = 'summary'
        with patch.object(ollama_agent, 'read', side_effect=AssertionError('no backend read')):
            self.assertEqual(OWNER_ONLY, local_intent.answer(matcher, "what's been happening"))

    def test_stop_microphone_still_works(self):
        model = Mock(return_value={'content': '', 'tool_calls': [{'function': {'name': 'stop_microphone', 'arguments': {}}}]})
        self.assertTrue(ollama_agent.Agent(chat=model).ask('quit listening').startswith("Okay, I'm turning"))


class Bridge(unittest.TestCase):
    """GET/POST /api/guest: the owner's switch, on the Eddie computer only."""

    def setUp(self):
        self.saved = voice_agent.profile, voice_agent.guest_timers
        voice_agent.profile = guest.Profile()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        voice_agent.profile, voice_agent.guest_timers = self.saved
        voice_agent.confirmation.cancel()

    def call(self, body=None):
        url = f'http://127.0.0.1:{self.server.server_address[1]}/api/guest'
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                         method='GET' if body is None else 'POST')
        try:
            with urllib.request.urlopen(request, timeout=3) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_owner_turns_it_on_and_picks_what_guests_use(self):
        status, view = self.call()
        self.assertEqual((200, False, []), (status, view['active'], view['allowed']))
        self.assertEqual(list(guest.APPROVABLE), view['approvable'])
        self.assertEqual((200, True), (lambda s, v: (s, v['active']))(*self.call({'active': True})))
        self.assertEqual(['timers', 'tv'], self.call({'allowed': ['tv', 'timers']})[1]['allowed'])
        self.assertEqual('guest', voice_agent.profile.role())

    def test_bad_changes_are_refused(self):
        for body in ({}, {'active': 'yes'}, {'allowed': ['traffic']}, {'role': 'owner'}, []):
            self.assertEqual(400, self.call(body)[0], body)
        self.assertEqual('owner', voice_agent.profile.role())

    def test_switching_drops_a_pending_yes_and_turning_off_forgets_guest_timers(self):
        voice_agent.confirmation.request('home_mode', {'enabled': True}, 'Turn home mode on?')
        self.call({'active': True})
        self.assertIsNone(voice_agent.confirmation.status())
        voice_agent.guest_timers.add('timer', voice_agent.guest_timers.clock() + 60, seconds=60)
        self.call({'allowed': ['timers']})
        self.assertEqual(1, len(voice_agent.guest_timers.pending()), 'changing approvals keeps them')
        self.call({'active': False})
        self.assertEqual([], voice_agent.guest_timers.pending())

    def test_not_a_paired_control(self):
        self.assertNotIn('/api/guest', serve.PAIRED_CONTROLS)


if __name__ == '__main__':
    unittest.main()
