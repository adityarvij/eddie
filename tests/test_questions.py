"""Ask before blocking (#79): tracker verdicts become rate-limited, grouped questions; answers write rules."""
import os
import time
import unittest
from unittest.mock import patch

from fastapi import WebSocketDisconnect

from eddie import api, decision, questions, rules
from tests import test_rules_api
from tests.test_dns_server import REAL_IP


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.q = questions.Questions(every=180, clock=self.clock)

    def test_a_domain_is_offered_once(self):
        self.assertTrue(self.q.offer('a.example', 'Analytics', 'TV'))
        self.assertFalse(self.q.offer('a.example', 'Analytics', 'Phone'))
        self.assertEqual(['a.example'], [d['domain'] for d in self.q.release(0)['domains']])
        self.clock.now += 1000
        self.assertFalse(self.q.offer('a.example', 'Analytics', 'TV'))
        self.assertIsNone(self.q.release(0))

    def test_one_open_question_and_one_every_few_minutes(self):
        self.q.offer('a.example', 'r', 'TV')
        first = self.q.release(0)
        self.q.offer('b.example', 'r', 'TV')
        self.assertIsNone(self.q.release(0))  # first still open
        self.q.take(first['id'])
        self.assertIsNone(self.q.release(0))  # answered, but too soon
        self.clock.now += 179
        self.assertIsNone(self.q.release(0))
        self.clock.now += 1
        self.assertEqual(['b.example'], [d['domain'] for d in self.q.release(0)['domains']])

    def test_waiting_trackers_are_grouped_by_device_busiest_first(self):
        for domain in ('a.example', 'b.example', 'c.example'):
            self.q.offer(domain, f'{domain} reason', 'TV')
        self.q.offer('d.example', 'r', 'Phone')
        question = self.q.release(0)
        self.assertEqual('TV', question['device'])
        self.assertEqual([{'domain': 'a.example', 'reason': 'a.example reason'},
                          {'domain': 'b.example', 'reason': 'b.example reason'},
                          {'domain': 'c.example', 'reason': 'c.example reason'}], question['domains'])
        self.assertEqual('3 new likely trackers on TV: a.example, b.example and c.example. Block them?',
                         question['prompt'])
        self.q.take(question['id'])
        self.clock.now += 180
        self.assertEqual('A new likely tracker on Phone: d.example. Block it?', self.q.release(0)['prompt'])

    def test_a_question_holds_at_most_five_domains(self):
        for i in range(7):
            self.q.offer(f'{i}.tracker.example', 'r', None)
        question = self.q.release(0)
        self.assertEqual(5, len(question['domains']))
        self.assertIn('on your network', question['prompt'])
        self.q.take(question['id'])
        self.clock.now += 180
        self.assertEqual(2, len(self.q.release(0)['domains']))

    def test_take_answers_once(self):
        self.q.offer('a.example', 'r', 'TV')
        qid = self.q.release(0)['id']
        self.assertIsNotNone(self.q.take(qid))
        self.assertIsNone(self.q.take(qid))
        self.assertEqual([], self.q.pending())


TRACKER = 'collector.px-cloud.example'


def verdict(domain=TRACKER, label='tracker'):
    return {'domain': domain, 'label': label, 'confidence': 0.9, 'reason': 'PerimeterX fingerprinting', 'ts': 1}


class QuestionAPITests(test_rules_api.RulesAPITests):
    """The question API against a real DNS server on one eddie.db (RulesAPITests' harness)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for p in (patch.object(api, 'asking', questions.Questions(every=180, clock=Clock())),
                  patch.object(api, 'recent', api.deque(maxlen=100))):
            p.start()
            self.closers.append(p.stop)

    def sent(self, kind):
        return [m for m in self.ws.sent if m['type'] == kind]

    async def ask_one(self):
        await self.http.post('/ingest', json={'ts': int(time.time() * 1000), 'device_mac': '172.20.10.5', 'device': 'Laptop',
                                              'domain': TRACKER, 'action': 'flag', 'source': 'ai'})
        self.assertEqual(200, (await self.http.post('/ai/verdict', json=verdict())).status_code)
        listed = (await self.http.get('/questions')).json()
        self.assertEqual(1, len(listed))
        return listed[0]

    async def test_a_tracker_verdict_asks_once(self):
        question = await self.ask_one()
        self.assertEqual(('Laptop', [{'domain': TRACKER, 'reason': 'PerimeterX fingerprinting'}]),
                         (question['device'], question['domains']))
        self.assertEqual([question], [{k: v for k, v in m.items() if k != 'type'} for m in self.sent('question')])
        await self.http.post('/ai/verdict', json=verdict())  # a repeat
        self.assertEqual(1, len(self.sent('question')))
        self.assertEqual([], rules.all_rules(api.rules_db()))  # asking writes nothing

    async def test_unsure_needed_and_plumbing_ask_nothing(self):
        await self.http.post('/ai/verdict', json=verdict('a.example', 'unsure'))
        await self.http.post('/ai/verdict', json=verdict('b.example', 'needed'))
        await self.http.post('/ai/verdict', json=verdict('_dns-sd._udp.example', 'tracker'))
        self.assertEqual([], (await self.http.get('/questions')).json())
        self.assertEqual([], self.sent('question'))

    async def test_block_creates_a_rule_and_the_next_lookup_is_blocked(self):
        await self.start_dns()
        self.assertEqual(REAL_IP, await self.ask(TRACKER))
        question = await self.ask_one()
        r = await self.http.post(f"/questions/{question['id']}", json={'answer': 'block', 'by': 'dashboard'})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertTrue(body['applied'])
        self.assertEqual([(TRACKER, 'block', 'global', 'dashboard')],
                         [(x['pattern'], x['action'], x['scope'], x['created_by']) for x in body['rules']])
        self.assertEqual('0.0.0.0', await self.ask(TRACKER))
        self.assertEqual(('block', 'rule'), (decision.evaluate(TRACKER, '127.0.0.1').action,
                                             decision.evaluate(TRACKER, '127.0.0.1').source))
        self.assertEqual([{'type': 'question_resolved', 'id': question['id'], 'answer': 'block', 'by': 'dashboard',
                           'applied': True}], self.sent('question_resolved'))
        self.assertEqual([], (await self.http.get('/questions')).json())
        # Undo (#15) is DELETE /rules/{id}: the lookup is forwarded again.
        d = await self.http.delete(f"/rules/{body['rules'][0]['id']}")
        self.assertTrue(d.json()['applied'])
        self.assertEqual(REAL_IP, await self.ask(TRACKER))

    async def test_with_dns_stopped_the_block_is_not_applied(self):
        question = await self.ask_one()
        with patch.object(api, 'APPLY_TIMEOUT', 0.05):
            r = await self.http.post(f"/questions/{question['id']}", json={'answer': 'block', 'by': 'voice'})
        self.assertFalse(r.json()['applied'])
        self.assertEqual('voice', r.json()['rules'][0]['created_by'])

    async def test_allow_writes_an_allow_rule(self):
        question = await self.ask_one()
        with patch.object(api, 'APPLY_TIMEOUT', 0):
            r = await self.http.post(f"/questions/{question['id']}", json={'answer': 'allow', 'by': 'dashboard'})
        self.assertEqual([(TRACKER, 'allow')], [(x['pattern'], x['action']) for x in r.json()['rules']])

    async def test_ignore_writes_no_rule_and_answers_once(self):
        question = await self.ask_one()
        r = await self.http.post(f"/questions/{question['id']}", json={'answer': 'ignore', 'by': 'dashboard'})
        self.assertEqual({'id': question['id'], 'answer': 'ignore', 'rules': [], 'applied': None}, r.json())
        self.assertEqual([], rules.all_rules(api.rules_db()))
        again = await self.http.post(f"/questions/{question['id']}", json={'answer': 'block', 'by': 'dashboard'})
        self.assertEqual(404, again.status_code)
        self.assertEqual([], rules.all_rules(api.rules_db()))

    async def test_bad_answers_are_refused(self):
        question = await self.ask_one()
        for body in ({'answer': 'delete', 'by': 'dashboard'}, {'answer': 'block', 'by': 'agent'}, {}):
            with self.subTest(body=body):
                self.assertEqual(422, (await self.http.post(f"/questions/{question['id']}", json=body)).status_code)
        self.assertEqual(1, len((await self.http.get('/questions')).json()))

    async def test_without_the_database_the_question_stays_open(self):
        question = await self.ask_one()
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}), patch.dict(api.db, {'conn': None}):
            r = await self.http.post(f"/questions/{question['id']}", json={'answer': 'block', 'by': 'dashboard'})
        self.assertEqual(503, r.status_code)
        self.assertEqual([question['id']], [q['id'] for q in (await self.http.get('/questions')).json()])

    async def test_snapshot_carries_open_questions(self):
        question = await self.ask_one()

        class Browser(test_rules_api.FakeSocket):
            headers = {}  # no Origin: not a browser page on another site

            async def accept(self):
                pass

            async def receive_text(self):
                raise WebSocketDisconnect()

        browser = Browser()
        await api.ws_events(browser)
        self.assertEqual([question], browser.sent[0]['questions'])


# The inherited rules tests already run in tests/test_rules_api.py.
for name in [n for n in dir(test_rules_api.RulesAPITests) if n.startswith('test_')]:
    setattr(QuestionAPITests, name, None)

if __name__ == '__main__':
    unittest.main()
