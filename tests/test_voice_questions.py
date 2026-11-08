"""Ask before blocking (#79) by voice: Eddie reads an open question on wake, and only a spoken yes blocks."""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent
from tests.test_voice_undo import FakeRules

TRACKER = 'collector.px-cloud.example'
QUESTION = {'id': 3, 'device': 'Laptop', 'domains': [{'domain': TRACKER, 'reason': 'Fingerprinting'}],
            'prompt': f'A new likely tracker on Laptop: {TRACKER}. Block it?', 'ts': 1}


class FakeAPI(FakeRules):
    """FakeRules plus the /questions contract: answering Block adds one voice rule per domain."""
    def __init__(self, applied=True):
        super().__init__(applied)
        self.open, self.answers = {QUESTION['id']: QUESTION}, []

    def __call__(self, path, body=None, timeout=3, method=None):
        if self.down:
            raise OSError('connection refused')
        if path == '/questions':
            return list(self.open.values())
        if path.startswith('/questions/'):
            self.answers.append(body)
            question = self.open.pop(int(path.rsplit('/', 1)[1]), None)
            if question is None:
                raise urllib.error.HTTPError(path, 404, 'Not Found', {}, io.BytesIO(b'{}'))
            added = [self.add(d['domain'], body['answer'], body['by']) for d in question['domains']]
            return {'id': question['id'], 'answer': body['answer'], 'rules': added, 'applied': self.applied}
        return super().__call__(path, body, timeout, method)


class VoiceQuestions(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        for target, value in (('request', self.api), ('confirmation', voice_agent.Confirmation()),
                              ('undo_stack', []), ('announced', set())):
            p = patch.object(voice_agent, target, value)
            p.start()
            self.addCleanup(p.stop)
        voice_agent.forget_conversation()

    def wake(self):
        return voice_agent.question_announcement()

    def test_wake_reads_the_question_once(self):
        self.assertEqual(f"I've got one question for you. {QUESTION['prompt']} Say yes to confirm or no to cancel.",
                         self.wake())
        self.assertEqual('question', voice_agent.confirmation.status()['kind'])
        voice_agent.confirmation.cancel()
        self.assertIsNone(self.wake())  # never asked about twice
        self.assertEqual([], self.api.answers)

    def test_yes_blocks_and_confirms_only_when_applied(self):
        self.wake()
        reply = voice_agent.respond('Yes.')
        self.assertEqual(f"Blocked {TRACKER} on every device. Eddie's DNS has loaded the change.", reply)
        self.assertEqual([{'answer': 'block', 'by': 'voice'}], self.api.answers)
        self.assertEqual([(TRACKER, 'block', 'voice')],
                         [(r['pattern'], r['action'], r['created_by']) for r in self.api.rows.values()])

    def test_with_dns_stopped_the_block_is_not_confirmed(self):
        self.api.applied = False
        self.wake()
        reply = voice_agent.respond('yes')
        self.assertNotIn('Blocked', reply)
        self.assertIn("isn't confirmed blocked", reply)

    def test_no_writes_nothing_and_leaves_the_question_open(self):
        self.wake()
        self.assertEqual('Cancelled. Nothing changed.', voice_agent.respond('no'))
        self.assertEqual([], self.api.answers)
        self.assertIn(QUESTION['id'], self.api.open)

    def test_undo_removes_the_block(self):
        self.wake()
        voice_agent.respond('yes')
        self.assertEqual(f"Undone. I removed the block on {TRACKER}, and Eddie's DNS has loaded the change.",
                         voice_agent.respond('undo that'))
        self.assertEqual({}, self.api.rows)

    def test_answered_elsewhere_changes_nothing(self):
        self.wake()
        self.api.open.clear()  # Ignore on the dashboard first
        self.assertEqual('That question was already answered. Nothing changed.', voice_agent.respond('yes'))
        self.assertEqual([], voice_agent.undo_stack)

    def test_backend_down_means_no_question(self):
        self.api.down = True
        self.assertIsNone(self.wake())
        self.assertIsNone(voice_agent.confirmation.status())


if __name__ == '__main__':
    unittest.main()
