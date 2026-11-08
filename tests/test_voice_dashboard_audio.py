"""EDDIE_SPEAKER: spoken replies on this speaker, as audio for the dashboard (the iPad), or both."""
import io
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve

RATE = 22050
SENTENCES = [np.full(RATE // 2, 100, dtype=np.int16), np.full(RATE // 4, -100, dtype=np.int16)]  # 0.75 s


class Piper:
    """piper.PiperVoice: two sentences of known audio."""
    @staticmethod
    def load(model):
        return Piper()

    def synthesize(self, text):
        return [types.SimpleNamespace(audio_int16_bytes=s.tobytes(), sample_rate=RATE) for s in SENTENCES]


class Speak(unittest.TestCase):
    def setUp(self):
        self.events, self.played = [], []
        patches = [patch.dict(sys.modules, {'piper': types.SimpleNamespace(PiperVoice=Piper),
                                            'sounddevice': types.SimpleNamespace()}),
                   patch.object(serve, 'emit', self.events.append),
                   patch.object(serve.playback, 'play', lambda sd, samples, rate: self.played.append((len(samples), rate))),
                   patch.object(serve.time, 'sleep')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sleep = serve.time.sleep
        serve.latest_audio.clear()
        serve.piper_voice.cache_clear()
        self.addCleanup(serve.piper_voice.cache_clear)

    def speak(self, speaker):
        with patch.object(serve, 'SPEAKER', speaker):
            serve.speak_piper('voice.onnx', 'Hello there.')

    def test_pi_plays_each_sentence_here_and_sends_no_audio(self):
        self.speak('pi')
        self.assertEqual([(RATE // 2, RATE), (RATE // 4, RATE)], self.played)
        self.assertEqual([], self.events)
        self.assertEqual({}, serve.latest_audio)

    def test_ipad_gets_one_clip_and_the_mic_waits_it_out(self):
        self.speak('ipad')
        self.assertEqual([], self.played)
        clip = serve.latest_audio
        self.assertEqual([{'type': 'speech_audio', 'id': clip['id']}], self.events)
        with wave.open(io.BytesIO(clip['wav'])) as file:
            self.assertEqual((1, 2, RATE, RATE // 2 + RATE // 4), (file.getnchannels(), file.getsampwidth(),
                                                          file.getframerate(), file.getnframes()))
        self.assertAlmostEqual(0.75 + serve.DASHBOARD_TAIL, self.sleep.call_args.args[0], places=3)

    def test_both_sends_the_clip_and_plays_it_here(self):
        self.speak('both')
        self.assertEqual('speech_audio', self.events[0]['type'])
        self.assertEqual([(RATE // 2 + RATE // 4, RATE)], self.played)
        self.sleep.assert_not_called()  # playing here already holds the mic for the reply's length

    def test_the_voice_loads_once(self):
        with patch.object(Piper, 'load', wraps=Piper.load) as load:
            self.speak('pi')
            self.speak('ipad')
        load.assert_called_once_with('voice.onnx')


class AudioEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}/api/voice/audio/'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def tearDown(self):
        serve.latest_audio.clear()

    def test_serves_only_the_newest_reply(self):
        serve.latest_audio.update(id='new', wav=b'RIFF-new')
        with urllib.request.urlopen(self.base + 'new') as response:
            self.assertEqual((b'RIFF-new', 'audio/wav', 'no-store'),
                             (response.read(), response.headers['Content-Type'], response.headers['Cache-Control']))
        for missing in ('old', 'new/../old', ''):
            with self.subTest(missing=missing), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self.base + missing)
            self.assertEqual(404, caught.exception.code)

    def test_nothing_to_serve_before_a_reply(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + 'anything')
        self.assertEqual(404, caught.exception.code)


if __name__ == '__main__':
    unittest.main()
