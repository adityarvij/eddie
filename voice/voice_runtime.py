"""Offline wake → bounded recording → transcription. Models are provisioned explicitly."""
import os
import queue
from pathlib import Path


class Capture:
    """16 kHz / 80 ms frames; ignore leading silence and stop after speech."""
    def __init__(self, threshold=0.015):
        self.threshold = threshold
        self.frames = []
        self.silent = 0
        self.heard = False

    def push(self, frame):
        import numpy as np
        self.frames.append(frame.copy())
        loud = np.sqrt(np.mean((frame.astype(np.float32) / 32768) ** 2)) >= self.threshold
        self.heard |= bool(loud)
        self.silent = 0 if loud else self.silent + 1
        return len(self.frames) >= 125 or (self.heard and self.silent >= 10)


def run(emit, answer, speaking, stopped):
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    import onnxruntime as ort
    ort.disable_telemetry_events()
    import numpy as np
    import sounddevice as sd
    from faster_whisper import WhisperModel

    wake_path = Path(os.environ.get('EDDIE_WAKE_MODEL', 'voice/models/hey_eddie.onnx')).resolve()
    whisper_path = Path(os.environ.get('EDDIE_WHISPER_MODEL', 'voice/models/whisper-base.en')).resolve()
    engine = os.environ.get('EDDIE_WAKE_ENGINE', 'openwakeword')
    if not whisper_path.is_dir():
        raise RuntimeError('Missing local Whisper model. See voice/README.md.')
    if engine == 'vosk':
        from wake_detector import VoskWake
        vosk_path = Path(os.environ.get('EDDIE_VOSK_MODEL', 'voice/models/vosk-model-small-en-us-0.15')).resolve()
        if not vosk_path.is_dir():
            raise RuntimeError('Missing local Vosk prototype model.')
        detector = VoskWake(vosk_path)
        ready_message = 'Offline prototype ready. Say “Hey Eddie,” pause for the overlay, then ask a question.'
    elif engine == 'openwakeword':
        from openwakeword.model import Model
        if not wake_path.is_file():
            raise RuntimeError('Missing custom Hey Eddie model. See voice/README.md; select EDDIE_WAKE_ENGINE=vosk for the explicit prototype.')
        detector = Model(wakeword_models=[str(wake_path)], inference_framework='onnx')
        ready_message = 'Local dedicated wake detector ready.'
    else:
        raise ValueError('EDDIE_WAKE_ENGINE must be openwakeword or vosk.')
    transcriber = WhisperModel(str(whisper_path), device='cpu', compute_type='int8', local_files_only=True)
    threshold = float(os.environ.get('EDDIE_WAKE_THRESHOLD', '0.5'))
    rms = float(os.environ.get('EDDIE_SPEECH_THRESHOLD', '0.015'))
    if not 0 < threshold <= 1 or not 0 < rms < 1:
        raise ValueError('Voice thresholds must be between zero and one.')
    audio = queue.Queue(maxsize=32)
    overflow = __import__('threading').Event()

    def callback(data, frames, timing, status):
        if stopped.is_set() or speaking.is_set():
            return
        if status:
            overflow.set()
        try:
            audio.put_nowait(data[:, 0].copy())
        except queue.Full:
            overflow.set()

    def drain():
        while True:
            try:
                audio.get_nowait()
            except queue.Empty:
                break
        detector.reset()

    device = os.environ.get('EDDIE_INPUT_DEVICE')
    if device and device.isdigit():
        device = int(device)
    with sd.InputStream(samplerate=16000, blocksize=1280, dtype='int16', channels=1, device=device, callback=callback):
        emit({'type': 'voice_status', 'status': 'ready', 'message': ready_message, 'engine': engine})
        capture = None
        hits = 0
        while not stopped.is_set():
            if speaking.is_set():
                stopped.wait(0.1)
                drain()
                capture = None
                continue
            try:
                frame = audio.get(timeout=0.2)
            except queue.Empty:
                continue
            if overflow.is_set():
                overflow.clear()
                drain()
                capture = None
                hits = 0
                emit({'type': 'voice_state', 'state': 'idle'})
                continue
            if capture is None:
                score = max(detector.predict(frame).values(), default=0)
                hits = hits + 1 if score >= threshold else 0
                if hits >= 2:
                    hits = 0
                    capture = Capture(rms)
                    emit({'type': 'wake'})
                    emit({'type': 'voice_state', 'state': 'listening'})
                continue
            if not capture.push(frame):
                continue
            if capture.heard:
                emit({'type': 'voice_state', 'state': 'thinking'})
                samples = np.concatenate(capture.frames).astype(np.float32) / 32768
                segments, _ = transcriber.transcribe(samples, language='en', beam_size=1, vad_filter=True)
                text = ' '.join(s.text.strip() for s in segments).strip()
                if text:
                    emit({'type': 'transcript', 'text': text})
                    # Synchronous reply prevents queued audio or speech re-triggering the detector.
                    answer(text)
            capture = None
            drain()
            emit({'type': 'voice_state', 'state': 'idle'})
