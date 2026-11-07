"""Local dashboard and offline voice bridge. No microphone audio is persisted."""
import argparse
import json
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = ROOT.parent / 'voice' / 'models' / 'vosk-model-small-en-us-0.15'
listeners = set()
lock = threading.Lock()
voice_lock = threading.Lock()
voice_state = {'status': 'off'}
voice_thread = None
speaking = threading.Event()
counts = {'requests': 0, 'blocked': 0}

def emit(event):
    with lock:
        for listener in list(listeners):
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass

def answer(text):
    # Placeholder summaries use only observed traffic; no invented device activity.
    if re.search(r'\b(block|allow|lock|mode|undo)\b', text.lower()):
        response = "My rule controls aren't connected yet. I haven't changed any network settings."
    elif re.search(r'\b(hello|hi|hey)\b', text.lower()):
        response = "Hey, I'm Eddie. I'm keeping an eye on things. What would you like to know?"
    else:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/summary', timeout=2) as summary:
                response = json.load(summary)['text']
        except Exception:
            response = f"I've seen {counts['requests']} requests and {counts['blocked']} blocked requests through my local event bridge this session."
    def speak():
        with voice_lock:
            speaking.set()
            emit({'type': 'speaking', 'text': response})
            try:
                if sys.platform == 'darwin':
                    subprocess.run(['/usr/bin/say', response], check=True, timeout=45)
                elif sys.platform == 'win32':
                    ps_cmd = 'Add-Type -AssemblyName System.Speech; [System.Speech.Synthesis.SpeechSynthesizer]::new().Speak([Console]::In.ReadToEnd())'
                    subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], input=response, text=True, check=True, timeout=45)
                else:
                    raise OSError('Speech not supported on this OS')
            except (OSError, subprocess.SubprocessError):
                emit({'type': 'voice_status', 'status': voice_state['status'], 'message': 'Speech output unavailable; Eddie’s response is shown on screen.'})
            finally:
                speaking.clear()
                emit({'type': 'speech_end'})
    threading.Thread(target=speak, daemon=True).start()

def microphone():
    try:
        import sounddevice as sd
        from vosk import Model, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        model = Model(str(MODEL))
        audio = queue.Queue(maxsize=64)
        def callback(data, frames, timing, status):
            try:
                audio.put_nowait(bytes(data))
            except queue.Full:
                pass
        rate = int(sd.query_devices(None, 'input')['default_samplerate'])
        recognizer = KaldiRecognizer(model, rate)
        active_until = 0
        last_wake = 0
        with sd.RawInputStream(samplerate=rate, blocksize=4000, dtype='int16', channels=1, callback=callback):
            voice_state['status'] = 'ready'
            emit({'type': 'voice_status', 'status': 'ready', 'message': 'Local microphone ready. Say “Hey Eddie,” pause, then ask a question.'})
            while True:
                data = audio.get()
                if speaking.is_set():
                    recognizer.Reset()
                    continue
                final = recognizer.AcceptWaveform(data)
                result = json.loads(recognizer.Result() if final else recognizer.PartialResult())
                text = result.get('text' if final else 'partial', '').strip()
                now = time.monotonic()
                match = re.search(r'\b(?:hey|hi|okay) (?:eddie|eddy|edie|ed)(?:\b)', text)
                if match and now - last_wake > 3:
                    last_wake = now
                    active_until = now + 12
                    emit({'type': 'wake'})
                    if not final:
                        continue
                if final and text and now < active_until:
                    command = re.sub(r'^.*?\b(?:hey|hi|okay) (?:eddie|eddy|edie|ed)\b', '', text).strip() if match else text
                    if command:
                        emit({'type': 'transcript', 'text': command})
                        answer(command)
                        active_until = 0
    except Exception as error:
        voice_state['status'] = 'error'
        emit({'type': 'voice_status', 'status': 'error', 'message': f'Microphone unavailable: {error}. Check microphone permissions for the app running this server.'})

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def json_response(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            events = queue.Queue(maxsize=200)
            with lock:
                listeners.add(events)
            events.put({'type': 'voice_status', **voice_state})
            try:
                while True:
                    try:
                        data = 'data: ' + json.dumps(events.get(timeout=15)) + '\n\n'
                    except queue.Empty:
                        data = ': heartbeat\n\n'
                    self.wfile.write(data.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                with lock:
                    listeners.discard(events)
        else:
            super().do_GET()

    def do_POST(self):
        global voice_thread
        # Reject cross-origin browser requests to this local control service.
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            return self.json_response(403, {'message': 'Local requests only.'})
        length = int(self.headers.get('Content-Length', '0'))
        if length > 65536:
            return self.json_response(413, {'message': 'Request too large.'})
        try:
            body = json.loads(self.rfile.read(length) or '{}')
        except (ValueError, UnicodeDecodeError):
            return self.json_response(400, {'message': 'Expected JSON.'})
        if self.path == '/api/voice/start':
            if not MODEL.exists():
                return self.json_response(503, {'message': 'Install the local voice model first; see frontend/README.md.'})
            if voice_thread and voice_thread.is_alive():
                return self.json_response(200, {'message': 'Local microphone is already listening.'})
            voice_state['status'] = 'starting'
            voice_thread = threading.Thread(target=microphone, daemon=True)
            voice_thread.start()
            return self.json_response(200, {'message': 'Starting the local microphone…'})
        if self.path == '/api/voice/ask':
            text = str(body.get('text', '')).strip()[:1000]
            if not text:
                return self.json_response(400, {'message': 'A question is required.'})
            answer(text)
            return self.json_response(200, {'ok': True})
        if self.path == '/api/events':
            if not isinstance(body, dict) or not body.get('domain'):
                return self.json_response(400, {'message': 'A domain is required.'})
            counts['requests'] += 1
            counts['blocked'] += str(body.get('action', body.get('decision', ''))).lower() in ('block', 'blocked')
            emit(body)
            return self.json_response(200, {'ok': True})
        self.json_response(404, {'message': 'Unknown endpoint.'})

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5173)
    args = parser.parse_args()
    print(f'Eddie dashboard: http://localhost:{args.port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
