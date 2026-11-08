"""Confirmation state for risky actions: only an explicit, timely yes executes."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'voice'))
sys.path.insert(0, str(ROOT / 'frontend'))
import voice_agent


class Clock:
    def __init__(self): self.now = 100.0
    def __call__(self): return self.now


def lockdown_command(text):
    if text.startswith('lock down the '):
        device = text.removeprefix('lock down the ')
        return 'lockdown', {'device': device}, f'Lock down the {device}?'


class ConfirmationFlow(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.action = Mock(side_effect=lambda device: f'{device} locked down.')
        patches = [
            patch.dict(voice_agent.RISKY_ACTIONS, {'lockdown': self.action}),
            patch.object(voice_agent, 'RISKY_COMMANDS', [lockdown_command]),
            # The generic flow, not the real lockdown parser (tests/test_voice_lockdown.py).
            patch.object(voice_agent, 'lockdown_target', lambda raw: None),
            patch.object(voice_agent, 'confirmation', voice_agent.Confirmation(clock=self.clock)),
            patch('voice_agent.request'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.request = voice_agent.request

    def ask(self):
        reply = voice_agent.respond('Lock down the TV.')
        self.assertEqual(reply, 'Lock down the tv? Yes or no?')
        self.action.assert_not_called()

    def test_yes_executes_once(self):
        self.ask()
        self.assertEqual(voice_agent.respond('Yes, do it!'), 'tv locked down.')
        self.action.assert_called_once_with(device='tv')
        self.assertIsNone(voice_agent.confirmation.status())
        # A second yes has nothing pending and must not repeat the action.
        voice_agent.respond('yes')
        self.action.assert_called_once()

    def test_no_discards(self):
        for phrase in ('No.', 'cancel', "Don't", 'never mind'):
            with self.subTest(phrase=phrase):
                self.ask()
                self.assertEqual(voice_agent.respond(phrase), 'Cancelled.')
                self.assertIsNone(voice_agent.confirmation.status())
        self.action.assert_not_called()

    def test_timeout_never_executes(self):
        self.ask()
        self.clock.now += 15
        self.assertIsNone(voice_agent.confirmation.status())
        self.assertIn('expired', voice_agent.respond('yes'))
        self.action.assert_not_called()
        self.assertIsNone(voice_agent.confirmation.pending)

    def test_expired_action_lets_other_questions_route_normally(self):
        self.request.return_value = {'text': 'Seven requests were blocked.'}
        self.ask()
        self.clock.now += 20
        self.assertEqual(voice_agent.respond("What's been going on?"), 'Seven requests were blocked.')
        self.action.assert_not_called()

    def test_dismissal_discards(self):
        self.ask()
        voice_agent.confirmation.cancel()
        self.assertEqual(voice_agent.NOT_CAUGHT, voice_agent.respond('yes'))
        self.action.assert_not_called()

    def test_new_command_replaces_pending_and_says_so(self):
        self.ask()
        reply = voice_agent.respond('lock down the fridge')
        self.assertTrue(reply.startswith('Lock down the fridge?'))
        self.assertEqual(voice_agent.respond('yes'), 'fridge locked down.')
        self.action.assert_called_once_with(device='fridge')

    def test_ambiguous_reprompts_once_then_discards(self):
        self.ask()
        self.assertEqual(voice_agent.respond('yes but not the tv'), 'Yes or no? Lock down the tv?')
        self.assertEqual(voice_agent.respond('maybe'), "No clear yes, so I didn't.")
        self.assertEqual(voice_agent.NOT_CAUGHT, voice_agent.respond('yes'))
        self.action.assert_not_called()
        self.request.assert_not_called()

    def test_missed_speech_counts_as_ambiguous(self):
        self.ask()
        self.assertIn('Yes or no?', voice_agent.respond(''))
        self.assertEqual("No clear yes, so I didn't.", voice_agent.respond(''))
        self.action.assert_not_called()

    def test_other_questions_while_pending_do_not_reach_backend(self):
        self.ask()
        self.assertIn('Yes or no?', voice_agent.respond('turn on home mode'))
        self.request.assert_not_called()

    def test_failed_action_is_not_reported_as_success(self):
        self.action.side_effect = OSError('offline')
        self.ask()
        self.assertIn("couldn't confirm", voice_agent.respond('yes'))

    def test_status_hides_arguments(self):
        self.ask()
        self.assertEqual(voice_agent.confirmation.status(), {'kind': 'lockdown', 'prompt': 'Lock down the tv?', 'expires_in_ms': 15000})

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            voice_agent.confirmation.request('wipe', {}, 'Wipe?')
        self.assertIsNone(voice_agent.confirmation.pending)


class BridgeEvents(unittest.TestCase):
    def setUp(self):
        import serve
        self.serve = serve
        self.events = []
        patches = [
            patch.object(serve, 'emit', self.events.append),
            patch.dict(voice_agent.RISKY_ACTIONS, {'lockdown': Mock()}),
            patch.object(serve, 'confirmation', voice_agent.Confirmation()),
            patch.dict(serve.voice_state, {'status': 'off'}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.confirmation = serve.confirmation

    def test_reply_pushes_confirming_state(self):
        def respond(text):
            return self.confirmation.request('lockdown', {'device': 'tv'}, 'Lock down the TV?')
        with patch.object(self.serve, 'respond', respond), patch.object(self.serve.subprocess, 'run'), \
                patch.object(self.serve.sys, 'platform', 'darwin'), patch.dict('os.environ', {'EDDIE_PIPER_MODEL': ''}):
            self.serve.answer('lock down the tv')
        last = self.events[-1]
        self.assertEqual((last['type'], last['state'], last['kind'], last['prompt']), ('voice_state', 'confirming', 'lockdown', 'Lock down the TV?'))
        self.assertNotIn('args', last)
        self.assertNotIn('device', last)

    def test_reply_without_pending_returns_to_idle(self):
        with patch.object(self.serve, 'respond', return_value='Hi.'), patch.object(self.serve.subprocess, 'run'), \
                patch.object(self.serve.sys, 'platform', 'darwin'), patch.dict('os.environ', {'EDDIE_PIPER_MODEL': ''}):
            self.serve.answer('hi')
        self.assertEqual(self.events[-1], {'type': 'voice_state', 'state': 'idle'})

    def test_worker_dismissal_and_stop_discard_pending(self):
        def run_voice(publish, answer, speaking, stopped):
            self.confirmation.request('lockdown', {'device': 'tv'}, 'Lock down the TV?')
            publish({'type': 'voice_state', 'state': 'dismissed', 'is_eddie_active': False})
            self.assertIsNone(self.confirmation.pending)
            self.confirmation.request('lockdown', {'device': 'tv'}, 'Lock down the TV?')
            raise RuntimeError('microphone unplugged')
        with patch.object(self.serve, 'run_voice', run_voice):
            self.serve.microphone()
        self.assertIsNone(self.confirmation.pending)


if __name__ == '__main__':
    unittest.main()
