import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve


class VoiceAgentSetting(unittest.TestCase):
    def test_ollama_agent_is_off_by_default_and_can_be_enabled(self):
        self.assertFalse(serve.ollama_agent_enabled({}))
        self.assertFalse(serve.ollama_agent_enabled({'EDDIE_AGENT': 'off'}))
        self.assertTrue(serve.ollama_agent_enabled({'EDDIE_AGENT': 'ollama'}))


if __name__ == '__main__':
    unittest.main()
