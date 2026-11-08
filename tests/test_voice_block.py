import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

DOMAIN = 'device-metrics-us.amazon.com'


def ack(pattern=DOMAIN, applied=True):
    return {'id': 7, 'scope': 'global', 'device_mac': None, 'pattern': pattern, 'action': 'block',
            'created_by': 'voice', 'ts': 1, 'applied': applied}


def event(domain, action, category='unknown', minutes_ago=1):
    return {'domain': domain, 'action': action, 'category': category, 'device': 'Fire Stick',
            'ts': int(time.time() * 1000) - minutes_ago * 60000}


def rule_call(domain=DOMAIN):
    return (('/rules', {'pattern': domain, 'action': 'block', 'created_by': 'voice'}), {'timeout': 5})


class BlockBase(unittest.TestCase):
    def setUp(self):
        voice_agent.forget_conversation()
        self.addCleanup(voice_agent.forget_conversation)


class Phrasing(BlockBase):
    def test_domain_variants(self):
        for phrase in (f'Block {DOMAIN}', f'block {DOMAIN}.', f'Please block {DOMAIN}',
                       f'Can you block {DOMAIN} please?', f'Okay Eddie, block {DOMAIN} now',
                       'block device-metrics-us dot amazon dot com', f'Go ahead and block the {DOMAIN}'):
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.block_target(phrase), ('domain', DOMAIN))

    def test_it_variants(self):
        for phrase in ('Block it', 'block it.', 'Block that.', 'block this one', 'Could you block that tracker?',
                       'block it please', "Eddie, block it for me"):
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.block_target(phrase), ('it', None))

    def test_not_a_block_command(self):
        for phrase in ("don't block it", 'should I block it', 'block it and email Will', 'why was it blocked',
                       f'unblock {DOMAIN}', 'what got blocked'):
            with self.subTest(phrase=phrase):
                self.assertIsNone(voice_agent.block_target(phrase))

    @patch('voice_agent.request')
    def test_negations_and_questions_never_write(self, request):
        for phrase in ("don't block it", f"Don't block {DOMAIN}", 'should I block it', 'block it and email Will'):
            voice_agent.respond(phrase)
        request.assert_not_called()


class BlockDomain(BlockBase):
    @patch('voice_agent.request', return_value=ack())
    def test_applied_reply(self, request):
        reply = voice_agent.respond(f'Block {DOMAIN}')
        self.assertEqual(reply, f"Blocked {DOMAIN}.")
        self.assertEqual(request.call_args_list, [rule_call()])

    @patch('voice_agent.request', return_value=ack(applied=False))
    def test_saved_but_not_applied_does_not_claim_blocked(self, request):
        reply = voice_agent.respond(f'block {DOMAIN}')
        self.assertEqual(voice_agent.SAVED_NOT_LOADED, reply)
        self.assertNotIn('Blocked', reply)

    @patch('voice_agent.request')
    def test_not_a_domain_asks_and_writes_nothing(self, request):
        for phrase in ('block netflix', 'block the TV', 'block device metrics'):
            with self.subTest(phrase=phrase):
                self.assertEqual(voice_agent.respond(phrase), voice_agent.NOT_RESOLVED)
        request.assert_not_called()


class BlockIt(BlockBase):
    @patch('voice_agent.request')
    def test_uses_last_domain_eddie_spoke_about(self, request):
        voice_agent.ask_model = lambda raw: f'The Fire Stick keeps calling {DOMAIN}, an Amazon tracker.'
        self.addCleanup(setattr, voice_agent, 'ask_model', None)
        voice_agent.respond('What is the TV doing?')
        request.return_value = ack()
        self.assertIn(f'Blocked {DOMAIN}', voice_agent.respond('Block it'))
        self.assertEqual(request.call_args_list, [rule_call()])

    @patch('voice_agent.request')
    def test_falls_back_to_newest_flagged_or_allowed_tracker(self, request):
        rows = [event('tracker.example.net', 'flag', minutes_ago=5),
                event(DOMAIN, 'allow', 'telemetry', minutes_ago=3),
                event('netflix.com', 'allow', 'streaming', minutes_ago=2),
                event('ads.example.org', 'block', 'ads', minutes_ago=1)]
        request.side_effect = [rows, ack()]
        self.assertIn(f'Blocked {DOMAIN}', voice_agent.respond('block that'))
        self.assertTrue(request.call_args_list[0][0][0].startswith('/events?since='))
        self.assertEqual(request.call_args_list[1], rule_call())

    @patch('voice_agent.request')
    def test_flagged_domain_resolves(self, request):
        request.side_effect = [[event('tracker.example.net', 'flag')], ack('tracker.example.net')]
        self.assertIn('Blocked tracker.example.net', voice_agent.respond('block it'))

    @patch('voice_agent.request', return_value=[event('netflix.com', 'allow', 'streaming'),
                                                event('ads.example.org', 'block', 'ads')])
    def test_unresolved_asks_and_changes_nothing(self, request):
        self.assertEqual(voice_agent.respond('Block it'), voice_agent.NOT_RESOLVED)
        self.assertEqual(request.call_count, 1)  # the /events read only, no POST

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_events_unreachable_changes_nothing(self, request):
        reply = voice_agent.respond('block it')
        self.assertEqual(voice_agent.UNREACHABLE, reply)
        self.assertEqual(request.call_count, 1)

    @patch('voice_agent.request')
    def test_ending_conversation_forgets_domain(self, request):
        request.return_value = ack()
        voice_agent.respond(f'block {DOMAIN}')
        voice_agent.forget_conversation()
        request.reset_mock(return_value=True)
        request.return_value = []
        self.assertEqual(voice_agent.respond('block it'), voice_agent.NOT_RESOLVED)

    @patch('voice_agent.request')
    def test_unresolved_prompt_does_not_become_it(self, request):
        request.return_value = []
        voice_agent.respond('block netflix')
        self.assertIsNone(voice_agent.context['domain'])


class Failures(BlockBase):
    def assert_failed(self, request, phrase=f'block {DOMAIN}'):
        self.assertEqual(voice_agent.respond(phrase), voice_agent.RULE_FAILED)

    @patch('voice_agent.request', side_effect=TimeoutError('timed out'))
    def test_timeout(self, request):
        self.assert_failed(request)

    @patch('voice_agent.request', side_effect=OSError('HTTP 503'))
    def test_backend_error(self, request):
        self.assert_failed(request)

    def test_missing_or_wrong_acknowledgement(self):
        for result in ({}, {'applied': True}, ack(pattern='other.com'), {**ack(), 'action': 'allow'},
                       {**ack(), 'id': '7'}):
            with self.subTest(result=result), patch('voice_agent.request', return_value=result):
                self.assert_failed(None)


if __name__ == '__main__':
    unittest.main()
