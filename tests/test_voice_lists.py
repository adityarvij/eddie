"""'List my rules' / 'list my devices': Eddie reads out what the dashboard's footer opens."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

RULES = [{'id': 3, 'pattern': 'unagi-na.amazon.com', 'action': 'block', 'device': None},
         {'id': 2, 'pattern': '*', 'action': 'block', 'device': 'Fire TV'},
         {'id': 1, 'pattern': 'netflix.com', 'action': 'allow', 'device': 'Fire TV'}]
DEVICES = {'devices': [{'label': None, 'vendor': 'TCL', 'queries': 40, 'blocked': 5},
                       {'label': "Adi's iPhone", 'vendor': 'Apple', 'queries': 242, 'blocked': 83},
                       {'label': None, 'vendor': None, 'queries': 3, 'blocked': 0}]}


def api(path, *args, **kwargs):
    return RULES if path == '/rules' else DEVICES


@patch('voice_agent.request', side_effect=api)
class Lists(unittest.TestCase):
    def test_rules_are_read_out(self, request):
        for said in ('List my rules', 'Hey Eddie, list my rules.', 'What are my rules?', 'Read me all my rules',
                     'Can you go through the rules?', 'What rules do I have?'):
            with self.subTest(said=said):
                self.assertEqual('You have 3 rules: block unagi-na.amazon.com, block everything on Fire TV '
                                 'and allow netflix.com on Fire TV.', voice_agent.route(said))

    def test_devices_are_read_out_busiest_first_by_name_never_mac(self, request):
        for said in ('List my devices', 'Show me my devices', 'What devices are on the network?', 'What devices do I have?'):
            with self.subTest(said=said):
                self.assertEqual("I've seen 3 devices today. Adi's iPhone made 242 lookups, 83 blocked; "
                                 "a TCL device made 40 lookups, 5 blocked; an unnamed device made 3 lookups, 0 blocked.",
                                 voice_agent.route(said))

    def test_a_sentence_never_starts_lowercase(self, request):
        with patch('voice_agent.request', return_value={'devices': [{'label': None, 'vendor': None, 'queries': 9, 'blocked': 1}]}):
            self.assertEqual("I've seen 1 device today. An unnamed device made 9 lookups, 1 blocked.",
                             voice_agent.route('list my devices'))

    def test_duplicate_rules_are_read_once(self, request):
        twice = [{'pattern': 'unagi-na.amazon.com', 'action': 'block', 'device': None}] * 2 + [RULES[2]]
        with patch('voice_agent.request', return_value=twice):
            self.assertEqual('You have 3 rules: block unagi-na.amazon.com and allow netflix.com on Fire TV.',
                             voice_agent.route('list my rules'))

    def test_long_lists_point_to_the_dashboard(self, request):
        many = [{'pattern': f'{n}.example', 'action': 'block', 'device': None} for n in range(8)]
        with patch('voice_agent.request', return_value=many):
            reply = voice_agent.route('list my rules')
        self.assertTrue(reply.startswith('You have 8 rules: block 0.example'))
        self.assertTrue(reply.endswith('and 3 more on the dashboard.'))

    def test_commands_that_mention_rules_are_not_lists(self, request):
        for said in ('block all my devices', 'delete my rules', 'list my rules and block it'):
            with self.subTest(said=said):
                self.assertFalse(voice_agent.LIST_RULES.fullmatch(voice_agent.words(said)))
                self.assertFalse(voice_agent.LIST_DEVICES.fullmatch(voice_agent.words(said)))


class Guest(unittest.TestCase):
    @patch('voice_agent.request', side_effect=AssertionError('a guest must not read the household lists'))
    def test_a_guest_gets_the_traffic_refusal(self, request):
        with patch.object(voice_agent.profile, 'check', return_value='That is for the owner.') as check:
            self.assertEqual('That is for the owner.', voice_agent.route('list my devices'))
            self.assertEqual('That is for the owner.', voice_agent.route('list my rules'))
        check.assert_called_with('traffic')


class Empty(unittest.TestCase):
    @patch('voice_agent.request', return_value=[])
    def test_no_rules(self, request):
        self.assertEqual("You don't have any rules yet. Say block, then a domain, to add one.", voice_agent.route('list my rules'))

    @patch('voice_agent.request', side_effect=OSError('down'))
    def test_backend_down_is_said(self, request):
        self.assertIn("can't reach Eddie's backend", voice_agent.route('list my devices'))
        self.assertIn("can't reach Eddie's backend", voice_agent.route('list my rules'))


if __name__ == '__main__':
    unittest.main()
