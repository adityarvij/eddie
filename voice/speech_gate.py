"""Local WebRTC speech decisions plus an in-memory ambient noise estimate."""
from collections import deque
import numpy as np

import playback


class SpeechGate:
    def __init__(self, aggressiveness=2, minimum_rms=0.003, margin=2.0, vad=None):
        if not 0 <= aggressiveness <= 3 or not 0 < minimum_rms < 1 or margin <= 1:
            raise ValueError('Invalid VAD calibration settings')
        if vad is None:
            import webrtcvad
            vad = webrtcvad.Vad(aggressiveness)
        self.vad = vad
        self.minimum_rms = minimum_rms
        self.margin = margin
        self.noise = deque(maxlen=125)

    @staticmethod
    def rms(frame):
        return float(np.sqrt(np.mean((frame.astype(np.float32) / 32768) ** 2)))

    def observe(self, frame):
        # The lower quintile avoids letting a brief conversation raise the
        # ambient estimate. Only scalar energies survive; no audio is retained.
        self.noise.append(self.rms(frame))

    @property
    def threshold(self):
        ambient = float(np.quantile(self.noise, .2)) if self.noise else 0
        return max(self.minimum_rms, ambient * self.margin)

    def __call__(self, frame):
        if frame.dtype != np.int16 or frame.shape != (1280,):
            raise ValueError('SpeechGate requires 80 ms of 16 kHz mono int16 PCM')
        votes = sum(self.vad.is_speech(frame[i:i+320].astype('<i2', copy=False).tobytes(),16000)
                    for i in range(0,1280,320))
        return votes >= 2 and self.rms(frame) >= self.threshold


def ready_cue(sd, suppressed, drain, stopped):
    """Gate capture through playback and its acoustic tail, then discard the queue."""
    suppressed.set()
    try:
        t = np.arange(1280, dtype=np.float32) / 16000
        tone = .12 * np.sin(2*np.pi*660*t) * np.sin(np.pi*np.arange(1280)/1279)**2
        playback.play(sd, tone.astype(np.float32), 16000)
        stopped.wait(.16)
    finally:
        drain()
        suppressed.clear()
