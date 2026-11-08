"""Plain-English rules through the local model (#108): a write tool call is schema-checked,
read back, and runs only after a spoken yes, through the same actions as voice commands."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import ollama_agent
import voice_agent
from test_voice_lockdown import TV, Clock, FakeBackend
from test_voice_ollama_agent import StubModel, call

NETFLIX = voice_agent.SERVICES['netflix']['patterns']


class Backend(FakeBackend):
    """FakeBackend plus POST /mode."""
    def __init__(self):
        super().__init__()
        self.modes = []

    def __call__(self, path, body=None, timeout=3, method=None):
        if path == '/mode' and not self.down:
            self.modes.append(body)
            return dict(body)
        return super().__call__(path, body, timeout, method)


class Writes(unittest.TestCase):
    def setUp(self):
        self.backend, self.clock = Backend(), Clock()
        for name, value in (('request', self.backend), ('undo_stack', []),
                            ('confirmation', voice_agent.Confirmation(clock=self.clock))):
            p = patch.object(voice_agent, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, voice_agent, 'ask_model', None)
        self.addCleanup(voice_agent.forget_conversation)

    def model(self, *replies):
        """Route unparsed requests to an agent scripted with replies."""
        self.stub = StubModel(*replies)
        bot = ollama_agent.Agent(url='http://127.0.0.1:11434', model='stub', chat=self.stub)
        voice_agent.ask_model = lambda text: ollama_agent.answer(bot, text)

    def wrote(self):
        return self.backend.posts or self.backend.modes

    def rules(self):
        return sorted((r['device_mac'], r['pattern'], r['action'], r['created_by'])
                      for r in self.backend.rows.values())

    # Valid

    def test_tv_only_netflix_is_read_back_then_locked_down_on_yes(self):
        self.model({'tool_calls': [call('lockdown_device', device='the TV', keep=['netflix'])]})
        reply = voice_agent.respond('The TV can only talk to Netflix.')
        self.assertEqual(reply, "Lock down the TV? It'll only reach Netflix, plus Eddie's essentials like sign-in "
                                "and updates. Say yes to confirm or no to cancel.")
        self.assertFalse(self.wrote())
        self.assertEqual(voice_agent.respond('yes'),
                         "The TV is locked down. The rules are saved and Eddie's DNS has loaded them. "
                         "I haven't seen its traffic blocked yet, and it only applies while it uses Eddie for DNS.")
        self.assertEqual(self.rules(), sorted([(TV, p, 'allow', 'voice') for p in NETFLIX]
                                              + [(TV, '*', 'block', 'voice')]))

    def test_lockdown_with_nothing_kept_reads_back_essentials_only(self):
        self.model({'tool_calls': [call('lockdown_device', device='Fire Stick', keep=[])]})
        self.assertEqual(voice_agent.respond('cut the fire stick off completely'),
                         "Lock down Fire Stick? It'll only reach Eddie's essentials like sign-in and updates. "
                         "Say yes to confirm or no to cancel.")

    def test_block_says_saved_but_not_loaded(self):
        self.backend.applied = False
        self.model({'tool_calls': [{'function': {'name': 'block',  # Ollama may send a JSON string
                                                 'arguments': json.dumps({'domain': 'Ads.Example.com.'})}}]})
        self.assertEqual(voice_agent.respond('stop that ad server on everything'),
                         'Block ads.example.com on every device? That adds one block rule. '
                         'Say yes to confirm or no to cancel.')
        self.assertEqual(voice_agent.respond('yes'), "I saved a rule to block ads.example.com, but Eddie's DNS "
                                                     "hasn't loaded it yet, so it may still get through.")
        self.assertEqual(self.rules(), [(None, 'ads.example.com', 'block', 'voice')])
        self.assertEqual(voice_agent.respond('undo that'),
                         "I removed the block on ads.example.com, but Eddie's DNS hasn't loaded the change yet, "
                         "so it may still apply for a moment.")

    def test_allow_service_and_domain(self):
        self.model({'tool_calls': [call('allow', service='youtube')]})
        reply = voice_agent.respond('let youtube through for everyone')
        self.assertTrue(reply.startswith('Allow YouTube on every device? That adds 7 allow rules'), reply)
        self.assertEqual(voice_agent.respond('yes'),
                         "Allowed YouTube on every device. The rules are saved and Eddie's DNS has loaded them.")
        self.model({'tool_calls': [call('allow', domain='api.example.com')]})
        self.assertEqual(voice_agent.respond('let the example api through'),
                         'Allow api.example.com on every device? That adds one allow rule. '
                         'Say yes to confirm or no to cancel.')
        self.assertEqual(voice_agent.respond('yes'),
                         "Allowed api.example.com on every device. The rule is saved and Eddie's DNS has loaded it.")

    def test_home_mode_never_claims_enforcement(self):
        self.model({'tool_calls': [call('set_mode', home_mode=True)]})
        self.assertEqual(voice_agent.respond("I'm home for the weekend"),
                         'Turn home mode on? Say yes to confirm or no to cancel.')
        self.assertEqual(self.backend.modes, [])
        self.assertEqual(voice_agent.respond('yes'), "Home mode is saved as on, but Eddie's DNS hasn't confirmed it, "
                                                     "so I can't say it's being enforced.")
        self.assertEqual(self.backend.modes, [{'home_mode': True}])

    # Invalid

    def test_malformed_write_calls_change_nothing(self):
        bad = [call('block', pattern='amazon.com'), call('block', domain='*.amazon.com'),
               call('block', domain=['a.com']), call('block'),
               call('allow', service='youtube', domain='a.com'), call('allow'), call('allow', service='hulu'),
               call('lockdown_device', device='TV', keep='netflix'),
               call('lockdown_device', device='TV', keep=['netflix', 'netflix']),
               call('lockdown_device', device='TV'), call('lockdown_device', device='the', keep=[]),
               call('set_mode', home_mode='true'), call('set_mode', home_mode=1),
               {'function': {'name': 'set_mode', 'arguments': '{not json'}}]
        for tool_call in bad:
            with self.subTest(call=tool_call):
                self.model({'tool_calls': [tool_call]})
                self.assertEqual(voice_agent.respond('do the thing'), ollama_agent.INVALID_WRITE)
                self.assertIsNone(voice_agent.confirmation.status())
        self.assertFalse(self.wrote())

    def test_two_writes_in_one_turn_change_nothing(self):
        self.model({'tool_calls': [call('set_mode', home_mode=False),
                                   call('lockdown_device', device='TV', keep=['netflix'])]})
        self.assertEqual(voice_agent.respond('turn off home mode and lock down the tv'), ollama_agent.ONE_WRITE)
        self.assertIsNone(voice_agent.confirmation.status())
        self.assertFalse(self.wrote())

    def test_unknown_device_is_said_not_proposed(self):
        self.model({'tool_calls': [call('lockdown_device', device='fridge', keep=['netflix'])]})
        reply = voice_agent.respond('the fridge can only use netflix')
        self.assertTrue(reply.startswith("I don't know a device called fridge."), reply)
        self.assertIsNone(voice_agent.confirmation.status())

    def test_model_claiming_a_change_is_overruled(self):
        self.model({'content': "Done, Netflix is now allowed."})
        self.assertEqual(voice_agent.respond('the tv should only get netflix'), ollama_agent.NOT_DONE)
        self.assertFalse(self.wrote())

    # Essentials

    def test_essentials_cannot_be_blocked(self):
        for domain in ('api.amazon.com', 'r3---sn-abc.googlevideo.com'):
            with self.subTest(domain=domain):
                self.model({'tool_calls': [call('block', domain=domain)]})
                self.assertEqual(voice_agent.respond('get rid of that'), voice_agent.essential_reply(domain))
                self.assertIsNone(voice_agent.confirmation.status())
                self.assertEqual(voice_agent.respond(f'block {domain}'), voice_agent.essential_reply(domain))
        self.assertFalse(self.wrote())

    def test_unreadable_allowlist_refuses_every_block(self):
        voice_agent.essentials.cache_clear()
        self.addCleanup(voice_agent.essentials.cache_clear)
        with patch.object(voice_agent, 'ALLOWLIST', Path('/nonexistent/allowlist.txt')):
            self.assertTrue(voice_agent.essential('ads.example.com'))
        voice_agent.essentials.cache_clear()
        self.assertFalse(voice_agent.essential('ads.example.com'))
        self.assertFalse(voice_agent.essential('amazon.com'))  # only api.amazon.com and others are essential

    # Declined and timed out

    def test_no_and_expiry_change_nothing(self):
        self.model({'tool_calls': [call('block', domain='ads.example.com')]})
        voice_agent.respond('kill that ad server')
        self.assertEqual(voice_agent.respond('no'), 'Cancelled. Nothing changed.')
        self.model({'tool_calls': [call('set_mode', home_mode=True)]})
        voice_agent.respond("I'm home")
        self.clock.now += voice_agent.CONFIRM_SECONDS
        self.assertEqual(voice_agent.respond('yes'),
                         "That request expired, so I didn't do it. Ask again if you still want it.")
        self.assertFalse(self.wrote())

    def test_model_timeout_falls_back_and_changes_nothing(self):
        self.model(TimeoutError('timed out'))
        self.assertEqual(voice_agent.respond('lock down the tv to netflix'),
                         "I haven't changed anything. I can block or allow a domain, allow a service like Netflix, "
                         "lock down a device, allow or deny a new device, undo my last rule, or turn home, away or kid mode on or off; email isn't "
                         "connected yet.")
        self.model(TimeoutError('timed out'))
        self.assertEqual(voice_agent.respond('block everything on the tv'), voice_agent.NOT_RESOLVED)
        self.assertIsNone(voice_agent.confirmation.status())
        self.assertFalse(self.wrote())


if __name__ == '__main__':
    unittest.main()
