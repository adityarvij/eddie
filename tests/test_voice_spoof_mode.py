import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))

from eddie import decision, spoof_mode, store
import voice_agent


class VoiceSpoofMode(unittest.TestCase):
    def test_on_and_off_phrases(self):
        for phrase, expected in (('turn on spoof mode', True), ('spoof mode on', True),
                                 ('switch spoof mode off', False), ('disable spoof mode', False)):
            with self.subTest(phrase=phrase):
                self.assertIs(voice_agent.spoof_mode_command(phrase), expected)

    @patch('voice_agent.request', return_value={'spoof_mode': True, 'spoof_applied': True})
    def test_voice_waits_for_backend_acknowledgement(self, request):
        self.assertEqual('Spoof mode turned on.', voice_agent.route('turn on spoof mode'))
        request.assert_called_once_with('/mode', {'spoof_mode': True})

    @patch('voice_agent.request', return_value={'spoof_mode': True, 'spoof_applied': False})
    def test_not_loaded_never_claims_mode_is_on(self, request):
        self.assertEqual("Spoof mode saved as on, but Eddie's DNS hasn't loaded it yet.",
                         voice_agent.route('turn on spoof mode'))

    @patch('voice_agent.request', side_effect=OSError('offline'))
    def test_failure_never_claims_a_change(self, request):
        self.assertEqual("I couldn't change spoof mode.", voice_agent.route('turn on spoof mode'))

    @patch('voice_agent.request')
    def test_questions_and_negations_do_not_write(self, request):
        for phrase in ("don't turn on spoof mode", 'is spoof mode on', 'turn on spoof mode and home mode'):
            voice_agent.route(phrase)
        request.assert_not_called()


class SpoofModeState(unittest.TestCase):
    def test_state_revision_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(Path(tmp) / 'eddie.db')
            rev = spoof_mode.write(conn, True)
            self.assertTrue(spoof_mode.wanted(conn))
            self.assertLess(spoof_mode.loaded(conn), rev)
            spoof_mode.mark_loaded(conn, rev)
            self.assertEqual(rev, spoof_mode.loaded(conn))

    def test_only_tracker_style_blocks_become_spoofed(self):
        engine = decision.Engine()
        engine.set_lists([], ['tracker.example'])
        engine.set_spoof_mode(True)
        self.assertEqual('spoof', engine.evaluate('tracker.example', 'phone').action)
        engine.set_rules([decision.Rule(1, 'explicit.example', 'block')])
        self.assertEqual('block', engine.evaluate('explicit.example', 'phone').action)


if __name__ == '__main__':
    unittest.main()
