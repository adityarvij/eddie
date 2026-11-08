"""Audio output at a rate the device takes: the Pi's USB speakerphone plays only 48 kHz."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'voice'))
import playback


class Speaker:
    """A sounddevice stand-in that, like the EMEET M1A on ALSA, plays only `rates`."""
    def __init__(self, rates=(48000,)):
        self.rates = rates
        self.played = []

    def check_output_settings(self, samplerate, channels):
        if samplerate not in self.rates:
            raise ValueError('Invalid sample rate [PaErrorCode -9997]')

    def query_devices(self, kind):
        return {'name': 'EMEET OfficeCore M1A', 'default_samplerate': 48000.0}

    def play(self, samples, rate):
        self.played.append((samples, rate))

    def wait(self):
        pass


class Play(unittest.TestCase):
    def test_piper_voice_is_resampled_for_a_48_khz_speaker(self):
        speaker = Speaker()
        voice = (np.sin(np.arange(22050) / 10) * 8000).astype(np.int16)  # 1 s at Piper's rate
        playback.play(speaker, voice, 22050)
        samples, rate = speaker.played[0]
        self.assertEqual(48000, rate)
        self.assertEqual(48000, len(samples))  # same duration
        self.assertEqual(np.int16, samples.dtype)
        self.assertLessEqual(np.abs(samples).max(), 8000)

    def test_the_16_khz_cue_plays_too(self):
        speaker = Speaker()
        playback.play(speaker, np.zeros(1280, dtype=np.float32), 16000)
        self.assertEqual((3840, 48000, np.float32), (len(speaker.played[0][0]), speaker.played[0][1],
                                                      speaker.played[0][0].dtype))

    def test_a_rate_the_device_takes_is_left_alone(self):
        speaker = Speaker(rates=(22050, 48000))
        voice = np.arange(100, dtype=np.int16)
        playback.play(speaker, voice, 22050)
        self.assertIs(voice, speaker.played[0][0])
        self.assertEqual(22050, speaker.played[0][1])


if __name__ == '__main__':
    unittest.main()
