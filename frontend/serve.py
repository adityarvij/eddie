"""Local dashboard and offline voice bridge. No microphone audio is persisted."""
import argparse
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'voice'))
import timers
import voice_agent
from voice_agent import confirmation, forget_conversation, kid_mode_change, question_announcement, request as api_request, respond
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
# Pairing token for control from another device (the iPad). Kept in memory; a new one each start
# unless EDDIE_DASHBOARD_TOKEN pins it so a saved Home Screen link keeps working.
token = os.environ.get('EDDIE_DASHBOARD_TOKEN') or secrets.token_urlsafe(24)
# Controls a paired device may use. Starting or stopping the microphone, and feeding traffic
# events, stay on the Eddie computer whatever token is sent.
PAIRED_CONTROLS = {'/api/voice/ask', '/api/mode', '/api/offline-mode', '/api/quarantine', '/api/devices/decide',
                   '/api/devices/update', '/api/kid-mode', '/api/questions/answer', '/api/questions/undo'}
# Read-only relays to the traffic API, so the page (and the iPad) need no CORS or direct API access.
READ_RELAYS = {'/api/rules': ('/rules', 'Rules unavailable.'),
               '/api/devices': ('/devices?hours=24', 'Devices unavailable.'),
               '/api/mode': ('/mode', 'Modes unavailable.'),
               '/api/questions': ('/questions', 'Questions unavailable.'),
               '/api/suggestions': ('/suggestions', 'Suggestions unavailable.')}
# Mode toggles: bridge path -> (API path, the one boolean field it takes).
# Rules written by dashboard answers (#79), question id -> rule rows, so its Undo removes only those.
# Memory only, at most ANSWERS_KEPT answers.
answered = {}
ANSWERS_KEPT = 20
MODE_CONTROLS = {'/api/mode': ('/mode', 'home_mode'), '/api/offline-mode': ('/offline-mode', 'offline'),
                 '/api/quarantine': ('/quarantine', 'quarantine')}
# Approve or deny a new device (#98): {mac, decision} -> POST /devices/<mac>/<decision>.
MAC = re.compile(r'[0-9a-f]{2}(?::[0-9a-f]{2}){5}')


def device_key(value):
    """A /devices key: a MAC, or a client IP while the MAC is unknown."""
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(MAC.fullmatch(value))

def is_local(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (getattr(ip, 'ipv4_mapped', None) or ip).is_loopback

def trusted_host(host):
    """The Host header names this machine by IP, localhost or mDNS, never a domain that could rebind to it."""
    name = re.sub(r':\d+$', '', host or '').strip('[]').rstrip('.').lower()
    if name == 'localhost' or name.endswith('.local'):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False

def authorized(header):
    scheme, _, value = (header or '').partition(' ')
    return scheme.lower() == 'bearer' and hmac.compare_digest(value.strip().encode(), token.encode())

def lan_ip():
    """This machine's address on its default route, or None when offline. Nothing is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))
        ip = s.getsockname()[0]
        return None if ip.startswith('127.') else ip
    except OSError:
        return None
    finally:
        s.close()

def emit(event):
    with lock:
        for listener in list(listeners):
            try:
                listener.put_nowait(event)
            except queue.Full:
                pass

def answer(text, reply=None, alarm=None):
    """alarm: a title ("Timer done"); the alarm sound plays first and the dashboard shows the title."""
    # Serialize both typed and spoken requests, including the full playback duration.
    with voice_lock:
        speaking.set()
        try:
            emit({'type': 'voice_state', 'state': 'thinking'})
            response = (reply or respond)(text)
            emit({'type': 'agent_reply', 'text': response})
            emit({'type': 'speaking', 'text': response, **({'title': alarm} if alarm else {})})
            if alarm:
                try:
                    timers.play_alarm()
                except Exception as error:
                    emit({'type': 'voice_status', 'status': voice_state['status'], 'message': f'Alarm sound unavailable: {error}'})
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

def ring_timers():
    """Ring timers and reminders as they come due (#122), whether or not the microphone is on."""
    while True:
        for item in voice_agent.timer_store.take_due():
            title, text = timers.announcement(item, datetime.now())
            answer(text, reply=str, alarm=title)
        time.sleep(1)

def microphone_off():
    """Stop the microphone (the voice "stop recording"); True if it was on. The thread ends after the reply."""
    was_on = voice_thread is not None and voice_thread.is_alive() and not stopped.is_set()
    stopped.set()
    return was_on

voice_agent.microphone_off = microphone_off

def microphone():
    try:
        def publish(event):
            if event.get('type') == 'voice_state' and event.get('state') == 'dismissed':
                confirmation.cancel()
                forget_conversation()
            if event.get('type') == 'voice_status':
                voice_state.update({key: value for key, value in event.items() if key != 'type'})
            emit(event)
            if event.get('type') == 'wake':
                # Ask before blocking (#79): an open question is read out once, as Eddie wakes.
                question = question_announcement()
                if question:
                    answer(question, reply=str)
        run_voice(publish, answer, speaking, stopped, dismissed)
    except Exception as error:
        voice_state['status'] = 'error'
        emit({'type': 'voice_status', 'status': 'error', 'message': f'Voice unavailable: {error}'})
    finally:
        # A pending risky action or remembered domain never outlives the microphone session.
        confirmation.cancel()
        forget_conversation()
        if stopped.is_set():
            voice_state['status'] = 'off'
            emit({'type': 'voice_status', 'status': 'off'})

def undo_answer(question_id):
    """(status, payload) after deleting the rules a dashboard answer wrote, newest first.

    A rule already gone, or whose id now holds another rule, is skipped. `applied` is DNS's
    acknowledgment of the last delete, as in DELETE /rules/{id}.
    """
    saved = answered.get(question_id)
    if not saved:
        return 404, {'message': 'Nothing to undo for that question.'}
    try:
        current = {row.get('id'): row for row in api_request('/rules')}
    except Exception:
        return 502, {'message': 'The traffic API did not answer. Nothing changed.'}
    removed, applied = 0, None
    while saved:
        rule = saved[-1]
        row = current.get(rule['id'])
        if row and row.get('created_by') == 'dashboard' and all(row.get(k) == rule.get(k) for k in ('pattern', 'action', 'ts')):
            try:
                result = api_request(f"/rules/{rule['id']}", method='DELETE', timeout=5)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code != 404:
                    break
            except Exception:
                break
            else:
                removed += 1
                applied = result.get('applied') is True
        saved.pop()
    if saved:
        return 502, {'message': f'Removed {removed} rule(s); the traffic API stopped answering. Try undo again.',
                     'removed': removed}
    answered.pop(question_id, None)
    return 200, {'removed': removed, 'applied': applied}


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
        if self.path in READ_RELAYS:
            path, failure = READ_RELAYS[self.path]
            try:
                return self.json_response(200, api_request(path))
            except Exception:
                return self.json_response(502, {'message': failure})
        if self.path in ('/api/what-they-know', '/api/what-they-know?demo=1'):
            # Read-only relay for the report panel; only these two fixed queries are forwarded.
            try:
                path = '/what-they-know?demo=true' if self.path.endswith('demo=1') else '/what-they-know'
                return self.json_response(200, api_request(path))
            except Exception:
                return self.json_response(502, {'message': 'Report unavailable.'})
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
        # A rebound domain would pass the Origin check below, so the Host must name this machine.
        if not trusted_host(self.headers.get('Host')):
            return self.json_response(403, {'message': 'Local requests only.'})
        # Reject cross-origin browser requests to this local control service.
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + self.headers.get('Host', ''):
            return self.json_response(403, {'message': 'Local requests only.'})
        # The Eddie computer itself is trusted. Another device (the iPad) needs the pairing token,
        # and even then only reaches PAIRED_CONTROLS: the microphone is never switched remotely.
        if not is_local(self.client_address[0]):
            if self.path not in PAIRED_CONTROLS:
                return self.json_response(403, {'message': 'This control only works on the Eddie computer.'})
            if not authorized(self.headers.get('Authorization')):
                return self.json_response(401, {'message': 'Pair this device with the link printed by the Eddie computer.'})
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
            forget_conversation()
            if voice_thread and voice_thread.is_alive():
                dismissed.set()
            return self.json_response(200, {'ok': True})
        if self.path == '/api/voice/ask':
            text = str(body.get('text', '')).strip()[:1000]
            if not text:
                return self.json_response(400, {'message': 'A question is required.'})
            threading.Thread(target=answer, args=(text,), daemon=True).start()
            return self.json_response(200, {'ok': True})
        if self.path in MODE_CONTROLS:
            # The answer is the API's own echo (offline mode adds DNS's `applied`); the page shows only that.
            path, field = MODE_CONTROLS[self.path]
            if not isinstance(body, dict) or not isinstance(body.get(field), bool):
                return self.json_response(400, {'message': f'{field} must be true or false.'})
            try:
                return self.json_response(200, api_request(path, {field: body[field]}, timeout=5))
            except Exception:
                return self.json_response(502, {'message': 'The traffic API did not confirm the change.'})
        if self.path == '/api/questions/answer':
            # The dashboard's Block / Allow / Ignore card; the API writes the rule and reports DNS's `applied`.
            if (not isinstance(body, dict) or type(body.get('id')) is not int
                    or body.get('answer') not in ('block', 'allow', 'ignore')):
                return self.json_response(400, {'message': 'Expected a question id and block, allow or ignore.'})
            try:
                result = api_request(f"/questions/{body['id']}", {'answer': body['answer'], 'by': 'dashboard'}, timeout=5)
                if result.get('rules'):
                    answered[body['id']] = result['rules']
                    while len(answered) > ANSWERS_KEPT:
                        answered.pop(next(iter(answered)))
                return self.json_response(200, result)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 404:
                    return self.json_response(404, {'message': 'That question was already answered.'})
                return self.json_response(502, {'message': 'The traffic API did not confirm the answer.'})
            except Exception:
                return self.json_response(502, {'message': 'The traffic API did not confirm the answer.'})
        if self.path == '/api/questions/undo':
            if not isinstance(body, dict) or type(body.get('id')) is not int:
                return self.json_response(400, {'message': 'Expected a question id.'})
            return self.json_response(*undo_answer(body['id']))
        if self.path == '/api/devices/decide':
            mac = body.get('mac') if isinstance(body, dict) else None
            decision = body.get('decision') if isinstance(body, dict) else None
            if not isinstance(mac, str) or not MAC.fullmatch(mac) or decision not in ('approve', 'deny'):
                return self.json_response(400, {'message': 'Expected a MAC address and approve or deny.'})
            try:
                return self.json_response(200, api_request(f'/devices/{mac}/{decision}', {}, timeout=5))
            except Exception:
                return self.json_response(502, {'message': 'The traffic API did not confirm the change.'})
        if self.path == '/api/devices/update':
            # Name a device, or mark it as a child's or an adult's (#119): {mac, label?, kid?} -> PUT /devices/<mac>.
            mac = body.get('mac') if isinstance(body, dict) else None
            change = {k: body[k] for k in ('label', 'kid') if isinstance(body, dict) and k in body}
            if (not device_key(mac) or not change or ('kid' in change and not isinstance(change['kid'], bool))
                    or ('label' in change and not isinstance(change['label'], (str, type(None))))):
                return self.json_response(400, {'message': 'Expected a device and a label or kid.'})
            try:
                return self.json_response(200, api_request(f'/devices/{mac}', change, timeout=5, method='PUT'))
            except urllib.error.HTTPError as error:
                error.close()
                if error.code == 422:
                    return self.json_response(422, {'message': 'Names are at most 40 printable characters.'})
                if error.code == 404:
                    return self.json_response(404, {'message': "Eddie hasn't seen that device."})
                return self.json_response(502, {'message': 'The traffic API did not confirm the change.'})
            except Exception:
                return self.json_response(502, {'message': 'The traffic API did not confirm the change.'})
        if self.path == '/api/kid-mode':
            # The page's own confirm dialog was the yes; the reply says only what the API acknowledged.
            if not isinstance(body, dict) or not isinstance(body.get('on'), bool):
                return self.json_response(400, {'message': 'on must be true or false.'})
            return self.json_response(200, {'message': kid_mode_change(body['on'])})
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
    # 0.0.0.0 lets the iPad view the dashboard; a paired iPad may ask, the mic stays localhost-only.
    parser.add_argument('--host', default=os.environ.get('EDDIE_DASHBOARD_HOST', '127.0.0.1'))
    args = parser.parse_args()
    if os.environ.get('EDDIE_AGENT', 'ollama') != 'off':
        import ollama_agent
        import voice_agent
        try:
            agent = ollama_agent.Agent()
            voice_agent.ask_model = lambda text: ollama_agent.answer(agent, text)
            voice_agent.phrase_inferences = lambda facts: ollama_agent.phrase(agent, facts)
            print(f'Local agent: {agent.model} at {agent.url} (keyword router if it is unavailable).', flush=True)
        except ValueError as error:
            print(f'Local agent off: EDDIE_OLLAMA_URL refused: {error}.', flush=True)
    timers_file = os.environ.get('EDDIE_TIMERS_FILE', str(ROOT.parent / 'voice' / 'timers.json'))
    voice_agent.timer_store = timers.Timers(None if timers_file == 'off' else timers_file)
    if voice_agent.timer_store.load_error:
        print(voice_agent.timer_store.load_error, flush=True)
    threading.Thread(target=ring_timers, daemon=True).start()
    print(f'Eddie dashboard: http://localhost:{args.port}', flush=True)
    if not is_local(args.host):
        # The token rides in the fragment, which browsers never send, so it stays out of request logs.
        # A pinned token outlives this run, so it isn't printed into a log (journald) that keeps it.
        address = lan_ip() or '<this computer IP>'
        shown = '<EDDIE_DASHBOARD_TOKEN>' if os.environ.get('EDDIE_DASHBOARD_TOKEN') else token
        print(f'Viewable from the network on port {args.port}. To ask Eddie from another device, open '
              f'http://{address}:{args.port}/#pair={shown} there. '
              'The microphone only works on this computer.', flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
