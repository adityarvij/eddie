import sys
import unittest
import threading
from pathlib import Path
from unittest.mock import Mock
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'voice'))
from speech_gate import SpeechGate, ready_cue
from voice_runtime import Capture

class VadTests(unittest.TestCase):
    def test_noisy_room_still_dismisses_and_click_does_not_start_speech(self):
        rng = np.random.default_rng(82)
        noise = [rng.normal(0,700,1280).astype(np.int16) for _ in range(80)]
        gate = SpeechGate()
        for frame in noise[:25]: gate.observe(frame)
        capture = Capture(speech_gate=gate)
        for frame in noise[25:74]: self.assertFalse(capture.push(frame))
        self.assertTrue(capture.push(noise[74]))
        self.assertFalse(capture.heard)
        click = np.zeros(1280,dtype=np.int16); click[600]=32000
        self.assertFalse(gate(click))

    def test_recorded_speech_in_noise_is_detected_then_endpointed(self):
        raw = np.fromfile(Path(__file__).parent/'fixtures/webrtc-test-audio.pcm',dtype='<i2')
        speech = np.interp(np.arange(len(raw)*2)/2,np.arange(len(raw)),raw).astype(np.int16)
        rng = np.random.default_rng(19)
        gate = SpeechGate()
        for _ in range(25): gate.observe(rng.normal(0,100,1280).astype(np.int16))
        c = Capture(speech_gate=gate)
        for start in range(0,len(speech)-1280,1280):
            frame=(speech[start:start+1280].astype(np.float32)+rng.normal(0,100,1280)).clip(-32768,32767).astype(np.int16)
            if c.push(frame): break
        self.assertTrue(c.heard)
        for _ in range(40):
            if c.push(np.zeros(1280,dtype=np.int16)): break
        self.assertGreaterEqual(c.silent,10)

    def test_cue_capture_is_suppressed_until_tail_and_drain(self):
        suppressed=threading.Event(); calls=[]
        sd=Mock()
        sd.play.side_effect=lambda *a: calls.append(('play',suppressed.is_set()))
        sd.wait.side_effect=lambda: calls.append(('wait',suppressed.is_set()))
        stopped=Mock()
        stopped.wait.side_effect=lambda t: calls.append(('tail',suppressed.is_set()))
        ready_cue(sd,suppressed,lambda:calls.append(('drain',suppressed.is_set())),stopped)
        self.assertEqual(calls,[('play',True),('wait',True),('tail',True),('drain',True)])
        self.assertFalse(suppressed.is_set())
