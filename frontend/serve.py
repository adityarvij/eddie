"""Local dashboard and offline voice bridge. No microphone audio is persisted."""
import argparse
import ipaddress
import json
import os
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
sys.path.insert(0, str(ROOT.parent / 'voice'))
from voice_agent import confirmation, request as api_request, respond
from voice_runtime import run as run_voice
stopped = threading.Event()
dismissed = threading.Event()  # the close button: end the voice conversation
listeners = set()
lock = threading.Lock()
voice_lock = threading.Lock()
voice_state = {'status': 'off'}
voice_thread = None
start_lock = threading.Lock()
speaking = threading.Event()
counts = {'requests': 0, 'blocked': 0}

def is_local(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (getattr(ip, 'ipv4_mapped', None) or ip).is_loopback

def emit(event):
    with lock:
        for listener in list(listeners):
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass

def answer(text):
    # Serialize both typed and spoken requests, including the full playback duration.
    with voice_lock:
        speaking.set()
        try:
            emit({'type': 'voice_state', 'state': 'thinking'})
            response = respond(text)
            emit({'type': 'agent_reply', 'text': response})
            emit({'type': 'speaking', 'text': response})
            piper_model = os.environ.get('EDDIE_PIPER_MODEL')
            if piper_model:
                from piper import PiperVoice
                import numpy as np
                import sounddevice as sd
                voice = PiperVoice.load(piper_model)
                for chunk in voice.synthesize(response):
                    sd.play(np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16), chunk.sample_rate)
                    sd.wait()
            elif sys.platform == 'darwin':
                subprocess.run(['/usr/bin/say', response], check=True, timeout=45)
            elif sys.platform == 'win32':
                ps_cmd = 'Add-Type -AssemblyName System.Speech; [System.Speech.Synthesis.SpeechSynthesizer]::new().Speak([Console]::In.ReadToEnd())'
                subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], input=response, text=True, check=True, timeout=45)
            else:
                raise RuntimeError('Configure EDDIE_PIPER_MODEL for local speech output.')
        except Exception as error:
            emit({'type': 'voice_status', 'status': voice_state['status'], 'message': f'Voice reply unavailable: {error}'})
        finally:
            speaking.clear()
            emit({'type': 'speech_end'})
            pending = confirmation.status()
            emit({'type': 'voice_state', 'state': 'confirming', **pending} if pending else {'type': 'voice_state', 'state': 'idle'})

def microphone():
    try:
        def publish(event):
            if event.get('type') == 'voice_state' and event.get('state') == 'dismissed':
                confirmation.cancel()
            if event.get('type') == 'voice_status':
                voice_state.update({key: value for key, value in event.items() if key != 'type'})
            emit(event)
        run_voice(publish, answer, speaking, stopped, dismissed)
    except Exception as error:
        voice_state['status'] = 'error'
        emit({'type': 'voice_status', 'status': 'error', 'message': f'Voice unavailable: {error}'})
    finally:
        # A pending risky action never outlives the microphone session.
        confirmation.cancel()
        if stopped.is_set():
            voice_state['status'] = 'off'
            emit({'type': 'voice_status', 'status': 'off'})

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
        if self.path == '/api/voice/status':
            return self.json_response(200, {'type': 'voice_status', **voice_state})
        if self.path == '/api/rules':
            # Read-only relay so the page (and the iPad) can list rules without the API allowing CORS.
            try:
                return self.json_response(200, api_request('/rules'))
            except Exception:
                return self.json_response(502, {'message': 'Rules unavailable.'})
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
        # The iPad may view the dashboard over the LAN, but controls stay on this machine
        # until the bridge has authenticated local control.
        if not is_local(self.client_address[0]):
            return self.json_response(403, {'message': 'Controls only work on the Eddie computer.'})
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
            with start_lock:
                if voice_thread and voice_thread.is_alive():
                    return self.json_response(200, {'message': 'Microphone is running or stopping; wait for its status.'})
                stopped.clear()
                dismissed.clear()
                voice_state['status'] = 'starting'
                voice_thread = threading.Thread(target=microphone, daemon=True)
                voice_thread.start()
            return self.json_response(200, {'message': 'Starting the local microphone…'})
        if self.path == '/api/voice/stop':
            stopped.set()
            return self.json_response(200, {'message': 'Stopping local microphone.'})
        if self.path == '/api/voice/dismiss':
            # Closing the overlay ends the conversation; the next request needs the wake phrase.
            confirmation.cancel()
            if voice_thread and voice_thread.is_alive():
                dismissed.set()
            return self.json_response(200, {'ok': True})
        if self.path == '/api/voice/ask':
            text = str(body.get('text', '')).strip()[:1000]
            if not text:
                return self.json_response(400, {'message': 'A question is required.'})
            threading.Thread(target=answer, args=(text,), daemon=True).start()
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
    # 0.0.0.0 lets the iPad view the dashboard; mic and other controls stay localhost-only.
    parser.add_argument('--host', default=os.environ.get('EDDIE_DASHBOARD_HOST', '127.0.0.1'))
    args = parser.parse_args()
    if os.environ.get('EDDIE_AGENT', 'ollama') != 'off':
        import ollama_agent
        import voice_agent
        try:
            agent = ollama_agent.Agent()
            voice_agent.ask_model = lambda text: ollama_agent.answer(agent, text)
            print(f'Local agent: {agent.model} at {agent.url} (keyword router if it is unavailable).', flush=True)
        except ValueError as error:
            print(f'Local agent off: EDDIE_OLLAMA_URL refused: {error}.', flush=True)
    print(f'Eddie dashboard: http://localhost:{args.port}', flush=True)
    if not is_local(args.host):
        print(f'Viewable from the network on port {args.port}; controls only work on this computer.', flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
