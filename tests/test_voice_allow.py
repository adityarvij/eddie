"""Voice "allow <service or domain>" (#13): services and wildcards need a yes, raw domains don't."""
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'voice'))
sys.path.insert(0, str(ROOT))
import voice_agent
from eddie import lists

DOMAIN = 'api.example.com'
NETFLIX = voice_agent.SERVICES['netflix']['patterns']
# eddie/rules.py PATTERN; copied so this test runs without the backend's dependencies.
RULE_PATTERN = re.compile(r'(\*\.)?[a-z0-9_-]+(\.[a-z0-9_-]+)+')


def ack(body, applied=True):
    return {'id': 7, 'scope': 'global', 'device_mac': None, 'pattern': body['pattern'],
            'action': body['action'], 'created_by': 'voice', 'ts': 1, 'applied': applied}


def acknowledge(applied=True):
    return lambda path, body=None, timeout=3: ack(body, applied)


def rule_call(pattern):
    return (('/rules', {'pattern': pattern, 'action': 'allow', 'created_by': 'voice'}), {'timeout': 5})


class AllowBase(unittest.TestCase):
    def setUp(self):
        voice_agent.forget_conversation()
        self.addCleanup(voice_agent.forget_conversation)
        p = patch.object(voice_agent, 'confirmation', voice_agent.Confirmation())
        p.start()
        self.addCleanup(p.stop)


class ServiceMap(unittest.TestCase):
    def test_every_pattern_is_a_valid_rule(self):
        for key, service in voice_agent.SERVICES.items():
            with self.subTest(service=key):
                self.assertTrue(service['name'] and service['aliases'] and service['patterns'])
                for pattern in service['patterns']:
                    self.assertTrue(RULE_PATTERN.fullmatch(pattern), pattern)

    def test_aliases_are_unique_and_already_normalized(self):
        aliases = [a for s in voice_agent.SERVICES.values() for a in s['aliases']]
        self.assertEqual(len(aliases), len(set(aliases)))
        for alias in aliases:
            self.assertRegex(alias, r'^[a-z0-9]+(?: [a-z0-9]+)*$')

    def test_no_pattern_unblocks_a_blocklisted_domain(self):
        _, blocked = lists.load()
        for service in voice_agent.SERVICES.values():
            for pattern in service['patterns']:
                base = pattern.removeprefix('*.')
                covered = {b for b in blocked
                           if b == base or (pattern.startswith('*.') and b.endswith('.' + base))}
                self.assertEqual(set(), covered, pattern)

    def test_required_services(self):
        for phrase, key in (('allow Netflix', 'netflix'), ('allow YouTube', 'youtube'),
                            ('allow you tube', 'youtube'), ('allow Prime Video', 'prime_video'),
                            ('allow Apple TV+', 'apple_tv'), ('allow the Apple TV plus', 'apple_tv')):
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.allow_target(phrase), ('service', key))


class Phrasing(unittest.TestCase):
    def test_targets(self):
        cases = {
            f'Allow {DOMAIN}': ('domain', DOMAIN),
            'can you allow api dot example dot com please?': ('domain', DOMAIN),
            'Okay Eddie, allow Netflix now': ('service', 'netflix'),
            'allow *.example.com': ('wildcard', '*.example.com'),
            'allow *.com': ('unknown', None),
            'allow Hulu': ('unknown', None),
            'allow it': ('unknown', None),
        }
        for phrase, expected in cases.items():
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.allow_target(phrase), expected)

    def test_not_an_allow_command(self):
        for phrase in ("don't allow Netflix", 'should I allow netflix', 'allow netflix and block it',
                       'why was it allowed', 'what got allowed'):
            with self.subTest(phrase=phrase):
                self.assertIsNone(voice_agent.allow_target(phrase))


class KnownService(AllowBase):
    @patch('voice_agent.request', side_effect=acknowledge())
    def test_confirm_then_allow_every_pattern(self, request):
        reply = voice_agent.respond('Allow Netflix')
        self.assertEqual(reply, f'Allow Netflix on every device? That adds {len(NETFLIX)} allow rules '
                                'for its domains. Say yes to confirm or no to cancel.')
        request.assert_not_called()
        reply = voice_agent.respond('yes')
        self.assertEqual(reply, "Allowed Netflix on every device. The rules are saved and Eddie's DNS has loaded them.")
        self.assertEqual(request.call_args_list, [rule_call(p) for p in NETFLIX])

    @patch('voice_agent.request')
    def test_no_or_unclear_writes_nothing(self, request):
        voice_agent.respond('allow youtube')
        self.assertEqual(voice_agent.respond('no'), 'Cancelled. Nothing changed.')
        voice_agent.respond('allow youtube')
        voice_agent.respond('maybe')
        self.assertEqual(voice_agent.respond('hmm'), "I didn't get a clear yes, so I didn't do it.")
        request.assert_not_called()

    @patch('voice_agent.request', side_effect=acknowledge(applied=False))
    def test_saved_but_not_loaded_does_not_claim_allowed(self, request):
        voice_agent.respond('allow prime video')
        reply = voice_agent.respond('yes')
        self.assertIn("hasn't loaded them yet", reply)
        self.assertNotIn('Allowed', reply)

    @patch('voice_agent.request', side_effect=acknowledge())
    def test_typed_wildcard_needs_confirmation(self, request):
        self.assertEqual(voice_agent.respond('allow *.example.com'),
                         'Allow example.com and every subdomain on every device? Say yes to confirm or no to cancel.')
        request.assert_not_called()
        self.assertEqual(voice_agent.respond('yes'), 'Allowed example.com and every subdomain on every device. '
                                                     "The rule is saved and Eddie's DNS has loaded it.")
        self.assertEqual(request.call_args_list, [rule_call('*.example.com')])

    def test_screen_status_has_no_patterns(self):
        voice_agent.respond('allow netflix')
        status = voice_agent.confirmation.status()
        self.assertEqual(status['kind'], 'allow')
        self.assertNotIn('patterns', status)


class RawDomain(AllowBase):
    @patch('voice_agent.request', side_effect=acknowledge())
    def test_allowed_without_confirmation(self, request):
        reply = voice_agent.respond(f'allow {DOMAIN}')
        self.assertEqual(reply, f"Allowed {DOMAIN} on every device. The rule is saved and Eddie's DNS has loaded it.")
        self.assertEqual(request.call_args_list, [rule_call(DOMAIN)])
        self.assertIsNone(voice_agent.confirmation.status())

    @patch('voice_agent.request', side_effect=acknowledge(applied=False))
    def test_saved_but_not_loaded(self, request):
        reply = voice_agent.respond(f'allow {DOMAIN}')
        self.assertIn(f'saved a rule to allow {DOMAIN}', reply)
        self.assertNotIn('Allowed', reply)


class UnknownService(AllowBase):
    @patch('voice_agent.request')
    def test_asks_instead_of_guessing(self, request):
        for phrase in ('allow Hulu', 'allow the TV', 'allow it', 'allow *.com'):
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.respond(phrase), voice_agent.ALLOW_UNRESOLVED)
                self.assertIsNone(voice_agent.confirmation.status())
        request.assert_not_called()


class BackendFailure(AllowBase):
    def test_raw_domain_failures(self):
        for effect in (TimeoutError('timed out'), OSError('HTTP 503'), [{}], [{**ack({'pattern': DOMAIN,
                       'action': 'block'})}], [ack({'pattern': 'other.com', 'action': 'allow'})]):
            with self.subTest(effect=effect), patch('voice_agent.request', side_effect=effect):
                self.assertEqual(voice_agent.respond(f'allow {DOMAIN}'), voice_agent.RULE_FAILED)

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_service_nothing_saved(self, request):
        voice_agent.respond('allow netflix')
        self.assertEqual(voice_agent.respond('yes'), voice_agent.RULE_FAILED)
        self.assertEqual(request.call_count, 1)  # stops at the first failure

    @patch('voice_agent.request')
    def test_service_partly_saved_says_so(self, request):
        request.side_effect = [ack({'pattern': NETFLIX[0], 'action': 'allow'}),
                               ack({'pattern': NETFLIX[1], 'action': 'allow'}), TimeoutError()]
        voice_agent.respond('allow netflix')
        reply = voice_agent.respond('yes')
        self.assertIn(f'only saved 2 of {len(NETFLIX)} allow rules for Netflix', reply)
        self.assertNotIn('Allowed', reply)
        self.assertEqual(request.call_count, 3)


if __name__ == '__main__':
    unittest.main()
