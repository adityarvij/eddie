"""Voice "undo" (#15): deletes only the rules voice itself created, newest command first."""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import voice_agent

DOMAIN = 'device-metrics-us.amazon.com'
NETFLIX = voice_agent.SERVICES['netflix']['patterns']


class FakeRules:
    """The /rules contract in memory. SQLite reuses a deleted top id, so this does too."""
    def __init__(self, applied=True):
        self.rows, self.applied, self.down, self.deletes = {}, applied, False, []
        self.fail_after = None  # answer this many DELETEs, then act as if the backend went away

    def add(self, pattern, action, created_by):
        rule_id = max(self.rows, default=0) + 1
        self.rows[rule_id] = {'id': rule_id, 'scope': 'global', 'device_mac': None, 'pattern': pattern,
                              'action': action, 'created_by': created_by, 'ts': 1000 + len(self.deletes) + rule_id}
        return dict(self.rows[rule_id])

    def __call__(self, path, body=None, timeout=3, method=None):
        if self.down:
            raise OSError('connection refused')
        if path == '/rules' and body is not None:
            return {**self.add(body['pattern'], body['action'], body['created_by']), 'applied': self.applied}
        if path == '/rules':
            return [dict(row) for row in sorted(self.rows.values(), key=lambda r: -r['id'])]
        if method == 'DELETE':
            if self.fail_after is not None and len(self.deletes) >= self.fail_after:
                raise OSError('timed out')
            rule_id = int(path.rsplit('/', 1)[1])
            self.deletes.append(rule_id)
            row = self.rows.pop(rule_id, None)
            if row is None:
                raise urllib.error.HTTPError(path, 404, 'Not Found', {}, io.BytesIO(b'{}'))
            return {**row, 'deleted': True, 'applied': self.applied}
        raise AssertionError(f'unexpected request {method} {path}')


class UndoBase(unittest.TestCase):
    def setUp(self):
        self.backend = FakeRules()
        for target, value in ((voice_agent, 'request'), (voice_agent, 'confirmation')):
            p = patch.object(target, value, self.backend if value == 'request' else voice_agent.Confirmation())
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(voice_agent, 'undo_stack', [])
        p.start()
        self.addCleanup(p.stop)
        voice_agent.forget_conversation()
        self.addCleanup(voice_agent.forget_conversation)

    def patterns(self):
        return sorted(row['pattern'] for row in self.backend.rows.values())


class Phrasing(unittest.TestCase):
    def test_undo_variants(self):
        for phrase in ('undo', 'Undo.', 'Undo that', 'Never mind, undo that', 'undo it please',
                       'Can you undo that?', 'Eddie, undo the last one', 'oops undo that'):
            with self.subTest(phrase=phrase):
                with patch.object(voice_agent, 'undo_command', return_value='undone') as undo:
                    self.assertEqual(voice_agent.respond(phrase), 'undone')
                    undo.assert_called_once()

    @patch('voice_agent.request')
    def test_not_an_undo(self, request):
        for phrase in ("don't undo that", 'undo that and block it', 'how do I undo that'):
            with self.subTest(phrase=phrase):
                with patch.object(voice_agent, 'undo_command') as undo:
                    voice_agent.respond(phrase)
                    undo.assert_not_called()
        request.assert_not_called()


class Undo(UndoBase):
    def test_nothing_to_undo(self):
        self.assertEqual(voice_agent.respond('undo'), "There's nothing for me to undo.")
        self.assertEqual(self.backend.deletes, [])

    def test_undo_after_block(self):
        voice_agent.respond(f'block {DOMAIN}')
        self.assertEqual(self.patterns(), [DOMAIN])
        reply = voice_agent.respond('Never mind, undo that')
        self.assertEqual(reply, f"Undid the block on {DOMAIN}.")
        self.assertEqual(self.backend.rows, {})
        self.assertEqual(voice_agent.respond('undo'), "There's nothing for me to undo.")

    def test_undo_after_allow_domain(self):
        voice_agent.respond('allow api.example.com')
        reply = voice_agent.respond('undo')
        self.assertEqual('Undid the allow rule for api.example.com.', reply)
        self.assertEqual(self.backend.rows, {})

    def test_undo_after_service_allow_removes_the_whole_set(self):
        voice_agent.respond('allow Netflix')
        voice_agent.respond('yes')
        self.assertEqual(len(self.backend.rows), len(NETFLIX))
        reply = voice_agent.respond('undo that')
        self.assertEqual(reply, "Undid the allow rules for Netflix.")
        self.assertEqual(self.backend.rows, {})

    def test_newest_first(self):
        voice_agent.respond('block a.example.com')
        voice_agent.respond('block b.example.com')
        self.assertIn('b.example.com', voice_agent.respond('undo'))
        self.assertEqual(self.patterns(), ['a.example.com'])
        self.assertIn('a.example.com', voice_agent.respond('undo'))
        self.assertEqual(voice_agent.respond('undo'), "There's nothing for me to undo.")

    def test_survives_the_conversation_ending(self):
        voice_agent.respond(f'block {DOMAIN}')
        voice_agent.forget_conversation()
        self.assertIn('Undid', voice_agent.respond('undo'))

    def test_not_loaded_does_not_claim_reverted(self):
        voice_agent.respond(f'block {DOMAIN}')
        self.backend.applied = False
        reply = voice_agent.respond('undo')
        self.assertIn("hasn't loaded the change", reply)
        self.assertNotIn('Undid', reply)

    def test_failed_block_leaves_nothing_to_undo(self):
        self.backend.down = True
        voice_agent.respond(f'block {DOMAIN}')
        self.backend.down = False
        self.assertEqual(voice_agent.respond('undo'), "There's nothing for me to undo.")


class AlreadyRemoved(UndoBase):
    def test_404_is_reported_and_dropped(self):
        voice_agent.respond('block a.example.com')
        voice_agent.respond(f'block {DOMAIN}')
        rule_id = max(self.backend.rows)
        listed = self.backend('/rules')
        del self.backend.rows[rule_id]  # removed from the dashboard after undo read the list
        with patch.object(voice_agent, 'request', side_effect=lambda path, *a, **k: (
                listed if path == '/rules' and k.get('method') is None and not a else self.backend(path, *a, **k))):
            reply = voice_agent.respond('undo')
        self.assertEqual(reply, 'That was already removed.')
        self.assertEqual(self.backend.deletes, [rule_id])
        self.assertIn('a.example.com', voice_agent.respond('undo'))  # the stale entry was dropped

    def test_missing_from_list_is_not_deleted(self):
        voice_agent.respond(f'block {DOMAIN}')
        self.backend.rows.clear()
        self.assertEqual('That was already removed.', voice_agent.respond('undo'))
        self.assertEqual(self.backend.deletes, [])
        self.assertEqual(voice_agent.respond('undo'), "There's nothing for me to undo.")


class OtherRules(UndoBase):
    def test_never_touches_dashboard_or_agent_rules(self):
        self.backend.add('dashboard.example.com', 'block', 'dashboard')
        voice_agent.respond(f'block {DOMAIN}')
        self.backend.add('agent.example.com', 'allow', 'agent')
        voice_agent.respond('undo')
        self.assertEqual(self.patterns(), ['agent.example.com', 'dashboard.example.com'])
        self.assertEqual(voice_agent.respond('undo'), "There's nothing for me to undo.")
        self.assertEqual(self.patterns(), ['agent.example.com', 'dashboard.example.com'])

    def test_reused_id_is_left_alone(self):
        voice_agent.respond(f'block {DOMAIN}')
        rule_id = max(self.backend.rows)
        del self.backend.rows[rule_id]  # deleted from the dashboard...
        self.assertEqual(self.backend.add('dashboard.example.com', 'block', 'dashboard')['id'], rule_id)  # ...id reused
        self.assertEqual('That was already removed.', voice_agent.respond('undo'))
        self.assertEqual(self.patterns(), ['dashboard.example.com'])
        self.assertEqual(self.backend.deletes, [])


class BackendDown(UndoBase):
    def test_down_changes_nothing_and_keeps_the_entry(self):
        voice_agent.respond(f'block {DOMAIN}')
        self.backend.down = True
        reply = voice_agent.respond('undo')
        self.assertEqual(reply, voice_agent.UNREACHABLE)
        self.backend.down = False
        self.assertIn('Undid', voice_agent.respond('undo'))
        self.assertEqual(self.backend.rows, {})

    def test_delete_fails(self):
        voice_agent.respond(f'block {DOMAIN}')
        self.backend.fail_after = 0
        self.assertEqual("I couldn't undo that.", voice_agent.respond('undo'))
        self.assertEqual(self.patterns(), [DOMAIN])

    def test_service_partly_removed_says_so_and_finishes_later(self):
        voice_agent.respond('allow Netflix')
        voice_agent.respond('yes')
        self.backend.fail_after = 2
        reply = voice_agent.respond('undo')
        self.assertEqual(reply, 'I only undid part of that; say undo again.')
        self.assertEqual(len(self.backend.rows), len(NETFLIX) - 2)
        self.backend.fail_after = None
        self.assertIn('Undid', voice_agent.respond('undo'))
        self.assertEqual(self.backend.rows, {})

    def test_unacknowledged_delete_is_not_reported_as_undone(self):
        voice_agent.respond(f'block {DOMAIN}')
        real = self.backend
        with patch.object(voice_agent, 'request', side_effect=lambda path, *a, **k: (
                {'id': 99} if k.get('method') == 'DELETE' else real(path, *a, **k))):
            reply = voice_agent.respond('undo')
        self.assertNotIn('Undid', reply)
        self.assertEqual("I couldn't undo that.", reply)


if __name__ == '__main__':
    unittest.main()
