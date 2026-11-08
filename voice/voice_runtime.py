"""Offline wake → bounded recording → transcription. Models are provisioned explicitly."""
import os
import queue
from pathlib import Path


class Capture:
    """16 kHz / 80 ms frames; wait up to 4 s for speech, then stop after 800 ms of silence."""
    def __init__(self, threshold=0.015, wait_frames=50, max_frames=125, speech_gate=None):
        self.speech_gate = speech_gate
        self.threshold = threshold
        self.wait_frames = wait_frames
        self.max_frames = max_frames
        self.frames = []
        self.waited = 0
        self.spoken = 0
        self.silent = 0
        self.heard = False

    def push(self, frame):
        import numpy as np
        loud = self.speech_gate(frame) if self.speech_gate else bool(np.sqrt(np.mean((frame.astype(np.float32) / 32768) ** 2)) >= self.threshold)
        self.heard |= loud
        self.frames.append(frame.copy())
        if not self.heard:
            # Keep a short pre-roll only, so waiting for speech stays bounded.
            self.frames = self.frames[-3:]
            self.waited += 1
            return self.waited >= self.wait_frames
        self.spoken += 1
        self.silent = 0 if loud else self.silent + 1
        return self.spoken >= self.max_frames or self.silent >= 10


def transcribe_command(samples, transcriber, emit, answer, retry=True):
    """Recover from a missed utterance without terminating wake detection.

    Returns the text. An empty result is answered ("Didn't catch that") only
    when retry is True, so background noise can't keep Eddie talking.
    """
    emit({'type': 'voice_state', 'state': 'thinking'})
    try:
        segments, _ = transcriber.transcribe(samples, language='en', beam_size=1, vad_filter=True)
        text = ' '.join(segment.text.strip() for segment in segments).strip()
    except Exception:
        text = ''
    if text:
        emit({'type': 'transcript', 'text': text})
    if text or retry:
        answer(text)
    return text


def run(emit, answer, speaking, stopped, dismissed=None):
    """dismissed: set by the dashboard's close button; ends the conversation so the next request needs the wake phrase."""
    dismissed = dismissed or __import__('threading').Event()
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    # Assets come from scripts/provision_voice.py; never let a library fetch at runtime.
    os.environ['HF_HUB_OFFLINE'] = '1'
    import onnxruntime as ort
    ort.disable_telemetry_events()
    import numpy as np
    import sounddevice as sd
    from faster_whisper import WhisperModel
    from speech_gate import SpeechGate, ready_cue
    speech_gate = SpeechGate(aggressiveness=int(os.environ.get('EDDIE_VAD_MODE', '2')),
                             minimum_rms=float(os.environ.get('EDDIE_SPEECH_THRESHOLD', '0.003')),
                             margin=float(os.environ.get('EDDIE_NOISE_MARGIN', '2.0')))

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
    rms = float(os.environ.get('EDDIE_SPEECH_THRESHOLD', '0.003'))
    if not 0 < threshold <= 1 or not 0 < rms < 1:
        raise ValueError('Voice thresholds must be between zero and one.')
    audio = queue.Queue(maxsize=32)
    overflow = __import__('threading').Event()
    cue_suppressed = __import__('threading').Event()

    def callback(data, frames, timing, status):
        if stopped.is_set() or speaking.is_set() or cue_suppressed.is_set():
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
        is_eddie_active = False
        missed = False  # the last follow-up transcribed to nothing

        def end_conversation():
            nonlocal capture, is_eddie_active, missed
            is_eddie_active = False
            capture = None
            missed = False
            drain()
            emit({'type': 'voice_state', 'state': 'dismissed', 'is_eddie_active': False})

        while not stopped.is_set():
            if dismissed.is_set():
                dismissed.clear()
                if is_eddie_active:
                    end_conversation()
                continue
            if speaking.is_set():
                stopped.wait(0.1)
                drain()
                capture = Capture(rms, speech_gate=speech_gate) if is_eddie_active else None
                continue
            try:
                frame = audio.get(timeout=0.2)
            except queue.Empty:
                continue
            if overflow.is_set():
                overflow.clear()
                drain()
                hits = 0
                if capture is not None:
                    capture = Capture(rms, speech_gate=speech_gate)
                continue
            if not is_eddie_active:
                speech_gate.observe(frame)
                score = max(detector.predict(frame).values(), default=0)
                hits = hits + 1 if score >= threshold else 0
                if hits >= 2:
                    hits = 0
                    capture = Capture(rms, speech_gate=speech_gate)
                    is_eddie_active = True
                    ready_cue(sd, cue_suppressed, drain, stopped)
                    emit({'type': 'wake'})
                    emit({'type': 'voice_state', 'state': 'listening', 'is_eddie_active': True})
                continue
            if not capture.push(frame):
                continue
            if not capture.heard:
                end_conversation()
                continue
            samples = np.concatenate(capture.frames).astype(np.float32) / 32768
            text = transcribe_command(samples, transcriber, emit, answer, retry=not missed)
            if stopped.is_set():  # "stop recording": the reply is spoken, the mic closes
                break
            # Closed while Eddie was thinking or speaking, or noise twice in a row: stop here.
            if dismissed.is_set() or (missed and not text):
                dismissed.clear()
                end_conversation()
                continue
            missed = not text
            # Keep listening for a follow-up without requiring the wake phrase again.
            drain()
            capture = Capture(rms, speech_gate=speech_gate)
            emit({'type': 'voice_state', 'state': 'listening', 'is_eddie_active': True})
