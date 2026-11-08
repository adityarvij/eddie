"""Local intent model (#179): traffic questions answered from the API without an LLM.

The classifier tests load the real pinned model (skipped without it); the answer and router tests
use a scripted classifier, so they check wiring and wording, not the embedding.
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import intents
import voice_agent

SUMMARY = {'text': 'In the last hour I saw 40 requests from 2 devices and blocked 12.',
           'devices': [{'device': 'Fire Stick', 'requests': 31, 'blocked': 12, 'flagged': 1,
                        'blocked_companies': [{'company': 'Amazon', 'blocked': 9}]},
                       {'device': "Adi's iPhone", 'requests': 9, 'blocked': 0, 'flagged': 0, 'blocked_companies': []}]}
NOW = time.time() * 1000


def api(path):
    """The traffic API as the read tools see it."""
    if path.startswith('/summary'):
        return SUMMARY
    if path.startswith('/events?action=block'):
        return [{'ts': NOW - 4 * 60000, 'device': 'Fire Stick', 'domain': 'device-metrics-us.amazon.com',
                 'company': 'Amazon', 'reason': 'StevenBlack blocklist'}]
    if path.startswith('/events?limit=1&domain=device-metrics-us.amazon.com'):
        return [{'ts': NOW - 4 * 60000, 'device': 'Fire Stick', 'action': 'block', 'company': 'Amazon',
                 'reason': 'StevenBlack blocklist'}]
    if path.startswith('/events'):
        return []
    if path == '/ai/verdicts':
        return []
    raise AssertionError(path)


class Scripted(intents.Intents):
    """Intents with a fixed classification instead of the model."""
    def __init__(self, intent):
        self.intent = intent

    def classify(self, text):
        return self.intent, 0.9


@patch('voice_agent.request', side_effect=api)
class Answers(unittest.TestCase):
    def setUp(self):
        voice_agent.context['domain'] = None

    def test_device_activity_uses_its_counts(self, request):
        reply = Scripted('activity')("What do you think the Fire Stick is up to?")
        self.assertEqual('Fire Stick made 31 requests in the last hour and Eddie blocked 12, 9 of them to Amazon.', reply)

    def test_tv_words_and_phone_words_find_the_device(self, request):
        self.assertIn('Fire Stick made 31', Scripted('activity')("What's the TV been doing?"))
        self.assertIn("Adi's iPhone made 9", Scripted('activity')("What's my phone doing?"))

    def test_a_device_eddie_has_not_seen_is_said_plainly(self, request):
        reply = Scripted('activity')("What's the doorbell doing?")
        self.assertEqual("I haven't seen a device called doorbell today. The devices I've seen today are "
                         "Fire Stick and Adi's iPhone.", reply)

    def test_the_whole_house_gets_the_summary(self, request):
        self.assertEqual(SUMMARY['text'], Scripted('activity')("What's been going on on the network?"))

    def test_recent_blocks_name_the_newest_for_follow_ups(self, request):
        reply = Scripted('blocks')('Anything blocked lately?')
        self.assertEqual('The latest block was device-metrics-us.amazon.com for Fire Stick, 4 minutes ago.', reply)
        self.assertEqual('device-metrics-us.amazon.com', voice_agent.context['domain'])

    def test_explain_reads_the_stored_decision(self, request):
        reply = Scripted('explain')('Why was device metrics us dot amazon dot com blocked?')
        self.assertEqual('Eddie blocked device-metrics-us.amazon.com, which belongs to Amazon, for Fire Stick '
                         '4 minutes ago. Reason: StevenBlack blocklist.', reply)

    def test_explain_that_means_the_last_domain_spoken_about(self, request):
        voice_agent.context['domain'] = 'device-metrics-us.amazon.com'
        self.assertIn('Reason: StevenBlack blocklist.', Scripted('explain')('Why did you block that?'))

    def test_explain_without_a_domain_asks_which(self, request):
        self.assertIn('Which domain?', Scripted('explain')('Why was it blocked?'))

    def test_no_intent_lets_the_router_carry_on(self, request):
        self.assertIsNone(Scripted(None)('Tell me a joke.'))
        request.assert_not_called()


class Failures(unittest.TestCase):
    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_backend_down_is_said_not_guessed(self, request):
        self.assertEqual(intents.BACKEND_FAILURE, Scripted('activity')("What's the TV doing?"))


class Router(unittest.TestCase):
    def setUp(self):
        self.calls = []
        voice_agent.understand = lambda raw: self.calls.append(raw) or 'understood'
        self.addCleanup(setattr, voice_agent, 'understand', None)

    def test_questions_reach_the_intent_model_before_any_llm(self):
        with patch.object(voice_agent, 'ask_model', side_effect=AssertionError('LLM called')):
            self.assertEqual('understood', voice_agent.route("What's the Fire Stick up to?"))

    def test_a_question_about_blocking_is_not_a_command(self):
        self.assertEqual('understood', voice_agent.route('What did you block today?'))

    def test_commands_never_reach_it(self):
        with patch('voice_agent.request', side_effect=OSError('offline')):
            voice_agent.route('Block the Fire Stick')
            voice_agent.route('Turn on home mode')
        self.assertEqual([], self.calls)

    @patch('voice_agent.what_they_know', return_value={'text': 'Not enough data yet.', 'enough_data': False})
    def test_what_they_know_keeps_its_own_route(self, report):
        self.assertEqual('Not enough data yet.', voice_agent.route('What do they know about me?'))
        self.assertEqual([], self.calls)


@unittest.skipUnless((intents.MODEL_DIR / 'model.onnx').exists(), 'needs scripts/provision_voice.py')
class Model(unittest.TestCase):
    """The real model on phrasings that aren't in EXAMPLES."""
    @classmethod
    def setUpClass(cls):
        cls.intents = intents.Intents()

    def intent(self, text):
        return self.intents.classify(' '.join(text.lower().replace('?', '').replace("'", '').split()))[0]

    def test_rephrasings_land_on_the_same_intent(self):
        for text, want in [("What do you reckon the Fire Stick's been up to?", 'activity'),
                           ('Is my iPhone chatting with anybody?', 'activity'),
                           ("What's going on with the network right now?", 'activity'),
                           ('Did Eddie stop anything in the last hour?', 'blocks'),
                           ('Which domains got blocked?', 'blocks'),
                           ('Why is device-metrics-us.amazon.com being blocked?', 'explain'),
                           ('What is this tracker for?', 'explain')]:
            with self.subTest(text=text):
                self.assertEqual(want, self.intent(text))

    def test_unrelated_questions_fall_through(self):
        for text in ("What's the weather tomorrow?", 'Sing me a song', 'Who won the game last night?',
                     'Remind me to buy milk', 'Is the front door locked?', 'What do they know about me?'):
            with self.subTest(text=text):
                self.assertIsNone(self.intent(text))

    def test_fast_enough_to_feel_instant(self):
        self.intent('warm up')
        started = time.monotonic()
        for _ in range(10):
            self.intent("What's the Fire Stick doing?")
        self.assertLess((time.monotonic() - started) / 10, 0.25)  # Pi 5 measurement in voice/README.md


if __name__ == '__main__':
    unittest.main()
