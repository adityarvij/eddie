import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import local_intent
import voice_agent


class SemanticRouting(unittest.TestCase):
    def tearDown(self):
        voice_agent.intent_model = None
        voice_agent.ask_model = None

    def matcher(self, intent):
        matcher = unittest.mock.Mock()
        matcher.match.return_value = intent
        return matcher

    @patch('ollama_agent.get_summary')
    def test_device_rephrasings_use_existing_device_parser_and_said_sentence(self, summary):
        summary.return_value = {'devices': [{'said': 'Fire Stick made 31 requests in the last hour and Eddie blocked 12.'}]}
        matcher = self.matcher('device_activity')
        for phrase, slot in (('what do you think the Fire Stick is doing', 'what do you think the Fire Stick is doing'),
                             ("what's my TV been up to", 'TV')):
            with self.subTest(phrase=phrase):
                self.assertIn('31 requests', local_intent.answer(matcher, phrase))
                summary.assert_called_with('1h', slot)

    @patch('ollama_agent.get_recent_blocks', return_value={'said': 'Recent blocks were ads.example on TV.'})
    def test_recent_blocks_returns_tool_sentence(self, blocks):
        self.assertEqual(local_intent.answer(self.matcher('recent_blocks'), 'what got stopped lately'),
                         'Recent blocks were ads.example on TV.')

    @patch('ollama_agent.explain_domain', return_value={'said': 'Eddie blocked ads.example because telemetry.'})
    def test_explanation_uses_spoken_domain_or_conversation_context(self, explain):
        matcher = self.matcher('why_blocked')
        self.assertIn('telemetry', local_intent.answer(matcher, 'why was ads.example blocked'))
        self.assertIn('telemetry', local_intent.answer(matcher, 'why was that blocked', 'ads.example'))
        self.assertEqual([c.args[0] for c in explain.call_args_list], ['ads.example', 'ads.example'])

    def test_below_threshold_falls_through(self):
        self.assertIsNone(local_intent.answer(self.matcher(None), 'tell me a joke'))

    @patch('voice_agent.request')
    def test_commands_and_near_misses_never_reach_question_matcher(self, request):
        seen = []
        voice_agent.intent_model = lambda text, domain=None: seen.append(text) or 'wrong'
        for phrase in ('block the Fire Stick', 'allow netflix.com', 'turn on home mode'):
            with self.subTest(phrase=phrase):
                voice_agent.respond(phrase)
        self.assertEqual([], seen)


class FakeEncoder:
    def encode(self, texts):
        rows = []
        for text in texts:
            rows.append([1, 0] if 'summary' in text or 'going on' in text else [0, 1])
        return np.asarray(rows, dtype=np.float32)


class MatcherThreshold(unittest.TestCase):
    def test_returns_best_intent(self):
        matcher = local_intent.Matcher(FakeEncoder(), threshold=.5)
        self.assertEqual('summary', matcher.match('give me a summary'))

    def test_negative_anchors_are_not_a_routable_intent(self):
        self.assertIn(None, local_intent.EXAMPLES)
        self.assertIn('block the Fire Stick', local_intent.EXAMPLES[None])


@unittest.skipUnless((local_intent.MODEL_DIR / 'model.onnx').exists(), 'needs scripts/provision_voice.py')
class RealModel(unittest.TestCase):
    """The pinned model on sentences that aren't in EXAMPLES (#192)."""
    @classmethod
    def setUpClass(cls):
        cls.matcher = local_intent.Matcher()

    def tearDown(self):
        voice_agent.intent_model = None

    def test_updates_and_house_check_ins_get_the_summary(self):
        for text in ('Give me an update', "How's the house doing?", 'Summarize the last hour',
                     'Anything interesting happening?', "What's up?", "What's up, Eddie?", "Hey Eddie, what's up?"):
            with self.subTest(text=text):
                self.assertEqual('summary', self.matcher.match(text))

    def test_other_rephrasings_route(self):
        for text, intent in (('Is my iPhone chatting with anybody?', 'device_activity'),
                             ('Did Eddie stop anything in the last hour?', 'recent_blocks'),
                             ('Which domains got blocked?', 'recent_blocks')):
            with self.subTest(text=text):
                self.assertEqual(intent, self.matcher.match(text))

    @patch('voice_agent.request', return_value={'text': 'x', 'devices': [
        {'device': 'Fire Stick', 'requests': 31, 'blocked': 12, 'flagged': 0, 'blocked_companies': []}]})
    def test_tv_control_and_listings_get_no_traffic_answer(self, request):
        voice_agent.intent_model = lambda text, domain=None: local_intent.answer(self.matcher, text, domain)
        for text in ('Turn the TV off', "What's on TV tonight?", 'Mute the TV', 'Lower the volume on the TV'):
            with self.subTest(text=text):
                self.assertIsNone(self.matcher.match(text))
                self.assertNotIn('requests', voice_agent.route(text))


if __name__ == '__main__':
    unittest.main()
