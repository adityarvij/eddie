"""Play mono audio at a rate the output device accepts.

A USB speakerphone often plays only 48 kHz (the EMEET M1A on the Pi), and the raw ALSA device
PortAudio opens there doesn't convert, so Piper's 22,050 Hz voice and the 16 kHz cues fail with
"Invalid sample rate". macOS converts on its own, so there the audio plays at its own rate.
"""


def output_rate(sd, rate):
    """rate if the default output takes it, else the device's own default rate."""
    check = getattr(sd, 'check_output_settings', None)
    if check is None:  # test doubles
        return rate
    try:
        check(samplerate=rate, channels=1)
        return rate
    except Exception:
        return int(sd.query_devices(kind='output')['default_samplerate'])


def resample(samples, rate, target):
    """Linear interpolation; plenty for speech and chimes going up to 48 kHz. Keeps the dtype."""
    import numpy as np  # here, not at import: serve.py and timers load this in the backend's numpy-free env
    samples = np.asarray(samples)
    if rate == target or not len(samples):
        return samples
    count = max(1, round(len(samples) * target / rate))
    out = np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples.astype(np.float32))
    return np.round(out).astype(samples.dtype) if samples.dtype.kind == 'i' else out.astype(samples.dtype)


def play(sd, samples, rate):
    """Play and wait until it has finished."""
    target = output_rate(sd, rate)
    sd.play(resample(samples, rate, target), target)
    sd.wait()
