import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import ollama_agent
import voice_agent

SUMMARY = {'home_mode': False, 'text': 'In the last hour I saw 40 requests from 2 devices and blocked 12.',
           'devices': [{'device': 'Fire Stick', 'requests': 31, 'blocked': 12, 'flagged': 1,
                        'blocked_companies': [{'company': 'Amazon', 'blocked': 9}]},
                       {'device': 'iPhone', 'requests': 9, 'blocked': 0, 'flagged': 0, 'blocked_companies': []}]}


def call(name, **arguments):
    return {'function': {'name': name, 'arguments': arguments}}


class StubModel:
    """Scripted model replies; records every message list it was sent."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def __call__(self, messages, timeout):
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def backend(path, body=None):
    if path.startswith('/summary'):
        return SUMMARY
    if path.startswith('/events'):
        return [{'ts': time.time() * 1000 - 120_000, 'device': 'Fire Stick', 'domain': 'device-metrics-us.amazon.com',
                 'company': 'Amazon', 'category': 'telemetry', 'action': 'block', 'source': 'blocklist',
                 'reason': 'On the Amazon Fire TV tracker list'}]
    if path == '/ai/verdicts':
        return []
    raise AssertionError(path)


def agent(*replies):
    model = StubModel(*replies)
    return ollama_agent.Agent(url='http://127.0.0.1:11434', model='stub', chat=model), model


@patch('voice_agent.request', side_effect=backend)
class Routing(unittest.TestCase):
    def test_tv_question_answers_with_real_counts(self, request):
        bot, model = agent({'content': '', 'tool_calls': [call('get_summary', window='1h', device='the TV')]},
                           {'content': 'The Fire Stick made 31 requests and I blocked 12, mostly Amazon.'})
        self.assertEqual(ollama_agent.answer(bot, "What's the TV doing?"),
                         'The Fire Stick made 31 requests and I blocked 12, mostly Amazon.')
        request.assert_called_once_with('/summary?window=1h')
        tool_message = model.seen[1][-1]
        self.assertEqual(tool_message['role'], 'tool')
        self.assertIn('"requests": 31', tool_message['content'])
        self.assertNotIn('iPhone', tool_message['content'])
        self.assertIn('Fire Stick made 31 requests in the last hour and Eddie blocked 12, 9 of them to Amazon.',
                      tool_message['content'])

    def test_all_devices_means_no_filter(self, request):
        for device in ('all', 'every device', ''):
            self.assertEqual(len(ollama_agent.get_summary('1h', device)['devices']), 2)
        ollama_agent.get_recent_blocks('all', 2)
        self.assertEqual(request.call_args.args[0], '/events?action=block&limit=2')

    def test_invented_number_is_replaced_by_backend_text(self, request):
        bot, _ = agent({'tool_calls': [call('get_summary', window='1h')]},
                       {'content': 'The TV tried 500 trackers.'})
        self.assertEqual(ollama_agent.answer(bot, 'how bad is it'), SUMMARY['text'])

    def test_numbers_without_any_tool_are_refused(self, request):
        bot, _ = agent({'content': 'Your TV sent 42 requests.'})
        self.assertEqual(ollama_agent.answer(bot, "What's the TV doing?"), ollama_agent.UNVERIFIED)
        request.assert_not_called()

    def test_spelled_out_numbers_are_checked(self, request):
        bot, _ = agent({'tool_calls': [call('get_recent_blocks', limit=1)]},
                       {'content': 'Seventeen blocks, all Amazon.'})
        self.assertEqual(ollama_agent.answer(bot, 'what got blocked'), ollama_agent.UNVERIFIED)

    def test_unknown_device_goes_back_to_model(self, request):
        bot, model = agent({'tool_calls': [call('get_summary', window='1h', device='fridge')]},
                           {'content': "I haven't seen a fridge, only the Fire Stick and iPhone."})
        self.assertIn('fridge', ollama_agent.answer(bot, "what's the fridge doing"))
        self.assertIn('known_devices', model.seen[1][-1]['content'])

    def test_recent_blocks_and_explain_domain(self, request):
        bot, model = agent({'tool_calls': [call('get_recent_blocks', device='Fire Stick', limit=3),
                                           call('explain_domain', domain='Device-Metrics-US.amazon.com.')]},
                           {'content': 'It blocked Amazon device metrics 2 minutes ago.'})
        self.assertEqual(ollama_agent.answer(bot, 'why was that blocked'),
                         'It blocked Amazon device metrics 2 minutes ago.')
        paths = [c.args[0] for c in request.call_args_list]
        self.assertIn('/events?action=block&limit=3&device=Fire%20Stick', paths)
        self.assertIn('/events?limit=1&domain=device-metrics-us.amazon.com', paths)
        self.assertIn('"minutes_ago": 2', model.seen[1][-2]['content'])

    def test_write_tools_are_offered_but_never_run(self, request):
        names = {tool['function']['name'] for tool in ollama_agent.TOOLS}
        self.assertEqual(names, {'get_summary', 'get_recent_blocks', 'explain_domain', 'what_they_know', 'burglars_eye',
                                 'block', 'allow', 'lockdown_device', 'set_mode'})
        bot, model = agent({'tool_calls': [call('block', pattern='amazon.com')]})
        self.assertEqual(ollama_agent.answer(bot, 'block amazon'), ollama_agent.INVALID_WRITE)
        self.assertEqual(len(model.seen), 1)  # a bad write ends the turn; the model doesn't retry
        request.assert_not_called()

    def test_bad_arguments_are_reported_to_model(self, request):
        bot, model = agent({'tool_calls': [call('what_they_know', hours=9000), call('get_summary', window='1y'),
                                           call('explain_domain', domain='not a domain'),
                                           call('get_summary', window='1h', extra=1)]},
                           {'content': 'Nothing to report.'})
        self.assertEqual(ollama_agent.answer(bot, 'hm'), 'Nothing to report.')
        errors = [m['content'] for m in model.seen[1] if m['role'] == 'tool']
        self.assertTrue(all('error' in e for e in errors), errors)
        request.assert_not_called()


class Failures(unittest.TestCase):
    @patch('voice_agent.request', side_effect=OSError('connection refused'))
    def test_tool_error_is_an_honest_spoken_failure(self, request):
        bot, model = agent({'tool_calls': [call('get_summary', window='1h', device='TV')]},
                           {'content': 'The TV made 31 requests.'})
        self.assertEqual(ollama_agent.answer(bot, "What's the TV doing?"), ollama_agent.BACKEND_FAILURE)
        self.assertEqual(len(model.seen), 1)  # the model never gets to phrase a guess

    @patch('voice_agent.request', side_effect=backend)
    def test_model_down_or_looping_falls_back(self, request):
        bot, _ = agent(OSError('ollama not running'))
        self.assertIsNone(ollama_agent.answer(bot, 'hi there'))
        loop = [{'tool_calls': [call('get_summary', window='1h')]}] * ollama_agent.MAX_ROUNDS
        bot, model = agent(*loop)
        self.assertIsNone(ollama_agent.answer(bot, 'hi there'))
        self.assertEqual(len(model.seen), ollama_agent.MAX_ROUNDS)

    def test_deadline_stops_further_model_calls(self):
        clock = iter([0, 0, 31]).__next__
        model = StubModel({'tool_calls': [call('get_recent_blocks', limit=200)]})
        bot = ollama_agent.Agent(url='http://localhost:11434', chat=model, clock=clock)
        self.assertIsNone(ollama_agent.answer(bot, 'x'))
        self.assertEqual(len(model.seen), 1)

    def test_only_local_model_urls(self):
        for url in ('http://8.8.8.8:11434', 'http://ollama.example.com', 'ftp://127.0.0.1', 'http://0.0.0.0:11434'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ollama_agent.Agent(url=url, chat=StubModel())
        ollama_agent.Agent(url='http://192.168.1.20:11434', chat=StubModel())


class Router(unittest.TestCase):
    def tearDown(self):
        voice_agent.ask_model = None

    @patch('voice_agent.request', side_effect=backend)
    def test_router_uses_model_for_questions_only(self, request):
        asked = []
        voice_agent.ask_model = lambda text: asked.append(text) or 'From the model.'
        self.assertEqual(voice_agent.respond("What's the TV doing?"), 'From the model.')
        self.assertEqual(asked, ["What's the TV doing?"])
        voice_agent.respond('turn on home mode')  # parsed commands never reach the model
        voice_agent.respond('hello')
        self.assertEqual(len(asked), 1)
        self.assertEqual(voice_agent.respond('block everything on the TV but Netflix'), 'From the model.')

    @patch('voice_agent.request', return_value={'text': 'Seven requests were blocked.'})
    def test_router_falls_back_when_model_unavailable(self, request):
        voice_agent.ask_model = lambda text: None
        self.assertEqual(voice_agent.respond("What's been going on?"), 'Seven requests were blocked.')


FACTS = [
    {'fact': 'Someone is up watching after midnight',
     'evidence': '1 streaming session started between midnight and 5 AM, 55 requests in all', 'confidence': 0.6},
    {'fact': 'Streaming happens most on Sundays',
     'evidence': '450 of 1691 streaming minutes in 9 sessions were on Sunday, across 7 days with streaming',
     'confidence': 0.65},
    {'fact': 'The house may have been empty or asleep for a stretch',
     'evidence': 'no streaming for 20h starting Tue 10:20 PM', 'confidence': 0.5},
]


class Inferences(unittest.TestCase):
    def model(self, content):
        seen = []

        def chat(messages, timeout, tools=None, num_predict=None):
            seen.append({'messages': messages, 'tools': tools})
            if isinstance(content, Exception):
                raise content
            return {'role': 'assistant', 'content': content}
        return ollama_agent.Agent(url='http://127.0.0.1:11434', model='stub', chat=chat), seen

    def test_model_sees_only_facts_and_no_tools(self):
        bot, seen = self.model('Someone could guess you stay up late: 1 session after midnight. Somewhat sure.')
        self.assertEqual(['Someone could guess you stay up late: 1 session after midnight. Somewhat sure.'],
                         bot.infer(FACTS))
        self.assertEqual([], seen[0]['tools'])
        self.assertIn('Using only these facts', seen[0]['messages'][0]['content'])
        self.assertEqual(FACTS, json.loads(seen[0]['messages'][1]['content']))

    def test_lines_with_unsupported_numbers_times_or_days_are_dropped(self):
        bot, _ = self.model('\n'.join([
            'Here is what I found:',
            '1. Someone could guess Sunday is TV day: 450 of 1691 minutes. Fairly sure.',
            '2. Someone could guess you watch 3 hours a night. Fairly sure.',  # 3 isn't in the facts
            '- Someone could guess the house empties at 10:20 PM on Tuesdays. A hunch.',
            '- Someone could guess the house empties at 9:15 PM. A hunch.',  # no such time
            '- Someone could guess Fridays are quiet. A hunch.',  # no Friday in the facts
            '- **Someone could guess** a night owl lives here, 60 percent sure.',
            'Nobody could guess much from 450 minutes.',  # not phrased as a guess
        ]))
        self.assertEqual([
            'Someone could guess Sunday is TV day: 450 of 1691 minutes. Fairly sure.',
            'Someone could guess the house empties at 10:20 PM on Tuesdays. A hunch.',
            'Someone could guess a night owl lives here, 60 percent sure.',
        ], bot.infer(FACTS))

    def test_at_most_five_lines(self):
        bot, _ = self.model('\n'.join(['Someone could guess people watch on Sundays.'] * 8))
        self.assertEqual(5, len(bot.infer(FACTS)))

    def test_phrase_falls_back_when_model_fails_or_nothing_survives(self):
        down, _ = self.model(OSError('connection refused'))
        self.assertIsNone(ollama_agent.phrase(down, FACTS))
        invented, _ = self.model('Someone could guess you own 4 TVs.')
        self.assertIsNone(ollama_agent.phrase(invented, FACTS))
        unused, seen = self.model('Someone could guess nothing.')
        self.assertIsNone(ollama_agent.phrase(unused, []))
        self.assertEqual([], seen, 'no facts, no model call')


class InferenceRouting(unittest.TestCase):
    REPORT = {'demo': False, 'hours': 168, 'facts': FACTS, 'text': 'Template text.'}

    def tearDown(self):
        voice_agent.phrase_inferences = None

    @patch('voice_agent.request', return_value=REPORT)
    def test_privacy_question_reads_all_retained_data_and_uses_the_model(self, request):
        given = []
        voice_agent.phrase_inferences = lambda facts: given.append(facts) or 'Someone could guess Sundays.'
        self.assertEqual('Someone could guess Sundays.', voice_agent.respond('What do they know about me?'))
        request.assert_called_once_with('/what-they-know')
        self.assertEqual([FACTS], given)

    @patch('voice_agent.request', return_value=REPORT)
    def test_template_when_model_has_nothing(self, request):
        voice_agent.phrase_inferences = lambda facts: None
        self.assertEqual('Template text.', voice_agent.respond('What do they know about me?'))

    @patch('voice_agent.request', return_value={**REPORT, 'facts': [], 'text': 'Not enough traffic.'})
    def test_no_facts_never_reaches_the_model(self, request):
        voice_agent.phrase_inferences = lambda facts: self.fail('model called without facts')
        self.assertEqual('Not enough traffic.', voice_agent.respond('What do they know about me?'))

    @patch('voice_agent.request', return_value={**REPORT, 'enough_data': False, 'text': 'Not enough data.'})
    def test_explicit_not_enough_data_never_reaches_the_model(self, request):
        voice_agent.phrase_inferences = lambda facts: self.fail('model called with insufficient data')
        self.assertEqual('Not enough data.', voice_agent.respond('What do they know about me?'))


if __name__ == '__main__':
    unittest.main()
