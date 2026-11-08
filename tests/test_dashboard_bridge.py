import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve
import voice_agent  # on sys.path once serve is imported


def lan_ip():
    """This machine's address on its default route, or None when offline."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))  # TEST-NET; nothing is sent
        ip = s.getsockname()[0]
        return None if ip.startswith('127.') else ip
    except OSError:
        return None
    finally:
        s.close()


class LocalAddress(unittest.TestCase):
    def test_only_loopback_is_local(self):
        for address in ('127.0.0.1', '127.0.0.2', '::1', '::ffff:127.0.0.1'):
            self.assertTrue(serve.is_local(address), address)
        for address in ('192.168.1.20', '172.20.10.3', '0.0.0.0', '::ffff:10.0.0.1', 'not an ip'):
            self.assertFalse(serve.is_local(address), address)


class PairingChecks(unittest.TestCase):
    def test_host_must_name_this_machine(self):
        for host in ('localhost:5173', '127.0.0.1:5173', '[::1]:5173', '192.168.1.20:5173', 'eddies-mac.local:5173', 'LOCALHOST'):
            self.assertTrue(serve.trusted_host(host), host)
        for host in ('evil.example:5173', 'localhost.evil.example', '', None):
            self.assertFalse(serve.trusted_host(host), host)

    def test_only_the_exact_bearer_token_authorizes(self):
        self.assertTrue(serve.authorized('Bearer ' + serve.token))
        self.assertTrue(serve.authorized('bearer ' + serve.token))
        for header in (None, '', serve.token, 'Bearer ', 'Bearer ' + serve.token + 'x', 'Basic ' + serve.token):
            self.assertFalse(serve.authorized(header), header)


@unittest.skipUnless(lan_ip(), 'needs a non-loopback network address')
class LanAccess(unittest.TestCase):
    """Dashboard bound to all interfaces: viewable from the LAN, controllable only locally."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('0.0.0.0', 0), serve.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, host, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f'http://{host}:{self.port}{path}', data=data, method='POST' if data else 'GET', headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_lan_can_view_dashboard_and_status(self):
        status, page = self.call(lan_ip(), '/')
        self.assertEqual(200, status)
        self.assertIn(b'<html', page.lower())
        self.assertEqual(200, self.call(lan_ip(), '/api/voice/status')[0])

    def test_lan_cannot_use_mic_or_feed_controls_even_paired(self):
        for headers in ({}, {'Authorization': 'Bearer ' + serve.token}):
            for path in ('/api/voice/start', '/api/voice/stop', '/api/voice/dismiss', '/api/events'):
                status, body = self.call(lan_ip(), path, {'text': 'hi', 'domain': 'x.com'}, headers)
                self.assertEqual(403, status, path)
                self.assertIn('Eddie computer', json.loads(body)['message'])
        self.assertFalse(serve.stopped.is_set())
        self.assertFalse(serve.dismissed.is_set())

    def test_lan_ask_needs_the_pairing_token(self):
        with patch.object(serve, 'answer') as answer:
            for headers in ({}, {'Authorization': 'Bearer wrong'}, {'Authorization': serve.token}):
                status, body = self.call(lan_ip(), '/api/voice/ask', {'text': 'hi'}, headers)
                self.assertEqual(401, status, headers)
                self.assertIn('Pair', json.loads(body)['message'])
            status, _ = self.call(lan_ip(), '/api/voice/ask', {'text': 'what is the TV doing?'},
                                  {'Authorization': 'Bearer ' + serve.token})
            self.assertEqual(200, status)
            for _ in range(50):  # answer runs on its own thread
                if answer.called:
                    break
                threading.Event().wait(0.02)
            answer.assert_called_once_with('what is the TV doing?')

    def test_lan_mode_toggles_need_the_pairing_token(self):
        with patch.object(serve, 'api_request', lambda path, body=None, timeout=3: dict(body)):
            for path, body in (('/api/mode', {'home_mode': True}), ('/api/offline-mode', {'offline': False})):
                status, reply = self.call(lan_ip(), path, body)
                self.assertEqual(401, status, path)
                self.assertIn('Pair', json.loads(reply)['message'])
                status, reply = self.call(lan_ip(), path, body, {'Authorization': 'Bearer ' + serve.token})
                self.assertEqual((200, body), (status, json.loads(reply)), path)

    def test_localhost_keeps_controls(self):
        status, _ = self.call('127.0.0.1', '/api/events', {'domain': 'example.com', 'action': 'block'})
        self.assertEqual(200, status)

    def test_rebound_domain_is_refused_on_localhost(self):
        # A page on evil.example rebound to 127.0.0.1 sends a matching Origin and Host.
        status, body = self.call('127.0.0.1', '/api/voice/start', {}, {'Host': 'evil.example', 'Origin': 'http://evil.example'})
        self.assertEqual(403, status)
        self.assertFalse(serve.voice_thread and serve.voice_thread.is_alive())


class RulesRelay(unittest.TestCase):
    """GET /api/rules relays the traffic API's rule list read-only, from any viewer."""

    def get(self, api_request, path='/api/rules'):
        server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.object(serve, 'api_request', api_request):
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}{path}', timeout=3) as r:
                    return r.status, json.load(r)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        finally:
            server.shutdown()
            server.server_close()

    def test_relays_the_rule_list(self):
        listed = [{'id': 1, 'pattern': 'a.example', 'action': 'block', 'device': 'TV', 'created_by': 'voice'}]
        self.assertEqual((200, listed), self.get(lambda path: listed if path == '/rules' else None))

    def test_api_failure_is_502_not_an_empty_list(self):
        def down(path):
            raise urllib.error.URLError('refused')
        self.assertEqual((502, {'message': 'Rules unavailable.'}), self.get(down))
        self.assertEqual((502, {'message': 'Devices unavailable.'}), self.get(down, '/api/devices'))
        self.assertEqual((502, {'message': 'Modes unavailable.'}), self.get(down, '/api/mode'))

    def test_relays_devices_and_modes(self):
        listed = {'hours': 24, 'devices': [{'mac': 'f0:4f:7c:11:22:33', 'name': 'Fire Stick', 'trackers': 2}]}
        mode = {'home_mode': False, 'offline': True, 'offline_applied': False}
        api = {'/devices?hours=24': listed, '/mode': mode}.get
        self.assertEqual((200, listed), self.get(api, '/api/devices'))
        self.assertEqual((200, mode), self.get(api, '/api/mode'))


class ModeControls(unittest.TestCase):
    """POST /api/mode and /api/offline-mode forward one boolean and answer with the API's own echo."""

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sent = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body, api_request):
        def record(api_path, payload=None, timeout=3):
            self.sent.append((api_path, payload))
            return api_request(api_path, payload)
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_address[1]}{path}',
                                         data=json.dumps(body).encode(), method='POST')
        with patch.object(serve, 'api_request', record):
            try:
                with urllib.request.urlopen(request, timeout=3) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

    def test_forwards_the_toggle_and_returns_the_echo(self):
        self.assertEqual((200, {'home_mode': True}),
                         self.post('/api/mode', {'home_mode': True}, lambda path, body: dict(body)))
        # Offline mode reports DNS's acknowledgment; the bridge passes `applied: false` through, never upgrades it.
        self.assertEqual((200, {'offline': True, 'applied': False}),
                         self.post('/api/offline-mode', {'offline': True}, lambda path, body: {**body, 'applied': False}))
        self.assertEqual([('/mode', {'home_mode': True}), ('/offline-mode', {'offline': True})], self.sent)

    def test_only_a_boolean_is_forwarded(self):
        for body in ({}, {'home_mode': 'yes'}, {'home_mode': 1}, {'offline': True}):
            status, reply = self.post('/api/mode', body, lambda path, payload: {})
            self.assertEqual(400, status, body)
            self.assertIn('home_mode', reply['message'])
        self.assertEqual([], self.sent)

    def test_api_failure_is_502_not_a_success(self):
        def down(path, body):
            raise urllib.error.URLError('refused')
        self.assertEqual((502, {'message': 'The traffic API did not confirm the change.'}),
                         self.post('/api/offline-mode', {'offline': True}, down))



class DeviceControls(unittest.TestCase):
    """POST /api/devices/update names a device or marks it a child's; /api/kid-mode runs kid mode (#119)."""

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sent = []
        self.answer = lambda path, body: {'mac': path.rsplit('/', 1)[1], **body}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def post(self, path, body):
        def record(api_path, payload=None, timeout=3, method=None):
            self.sent.append((method, api_path, payload))
            return self.answer(api_path, payload)
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_address[1]}{path}',
                                         data=json.dumps(body).encode(), method='POST')
        with patch.object(serve, 'api_request', record):
            try:
                with urllib.request.urlopen(request, timeout=3) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

    def test_label_and_kid_are_forwarded_as_a_put(self):
        self.assertEqual(200, self.post('/api/devices/update', {'mac': 'aa:bb:cc:dd:ee:01', 'label': 'Tablet'})[0])
        self.assertEqual(200, self.post('/api/devices/update', {'mac': '172.20.10.9', 'kid': True})[0])
        self.assertEqual([('PUT', '/devices/aa:bb:cc:dd:ee:01', {'label': 'Tablet'}),
                          ('PUT', '/devices/172.20.10.9', {'kid': True})], self.sent)

    def test_bad_bodies_reach_nothing(self):
        for body in ({'mac': 'aa:bb:cc:dd:ee:01'}, {'mac': '../rules', 'label': 'x'}, {'label': 'x'},
                     {'mac': 'aa:bb:cc:dd:ee:01', 'kid': 'yes'}, {'mac': 'aa:bb:cc:dd:ee:01', 'label': 3}):
            self.assertEqual(400, self.post('/api/devices/update', body)[0], body)
        self.assertEqual([], self.sent)

    def test_api_refusals_are_explained(self):
        def refuse(code):
            def answer(path, body):
                raise urllib.error.HTTPError(path, code, 'no', {}, None)
            return answer
        self.answer = refuse(422)
        self.assertEqual(422, self.post('/api/devices/update', {'mac': 'aa:bb:cc:dd:ee:01', 'label': 'x' * 41})[0])
        self.answer = refuse(404)
        self.assertEqual(404, self.post('/api/devices/update', {'mac': 'aa:bb:cc:dd:ee:01', 'label': 'TV'})[0])

    def test_kid_mode_answers_with_what_the_api_acknowledged(self):
        with patch.object(serve, 'kid_mode_change', lambda on: f'kid mode {on}'):
            self.assertEqual((200, {'message': 'kid mode True'}), self.post('/api/kid-mode', {'on': True}))
            self.assertEqual(400, self.post('/api/kid-mode', {'on': 'yes'})[0])

    def test_a_paired_device_may_use_them(self):
        self.assertLessEqual({'/api/devices/update', '/api/kid-mode'}, serve.PAIRED_CONTROLS)

class Dismiss(unittest.TestCase):
    """The close button ends the worker's conversation, not just the overlay."""

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(serve.dismissed.clear)
        self.addCleanup(setattr, serve, 'voice_thread', serve.voice_thread)

    def dismiss(self):
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_address[1]}/api/voice/dismiss', data=b'', method='POST')
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status

    def test_signals_a_running_microphone(self):
        alive = threading.Event()
        serve.voice_thread = threading.Thread(target=alive.wait)
        serve.voice_thread.start()
        self.addCleanup(alive.set)
        with patch.dict(voice_agent.RISKY_ACTIONS, {'lockdown': Mock()}):
            serve.confirmation.request('lockdown', {}, 'Lock down the TV?')
        self.assertEqual(200, self.dismiss())
        self.assertTrue(serve.dismissed.is_set())
        self.assertIsNone(serve.confirmation.status())

    def test_without_a_microphone_nothing_is_left_pending(self):
        serve.voice_thread = None
        self.assertEqual(200, self.dismiss())
        self.assertFalse(serve.dismissed.is_set())


if __name__ == '__main__':
    unittest.main()


class QuestionControls(unittest.TestCase):
    """The dashboard's question card (#79): answers go to the API as `by: dashboard`; Undo removes only those rules."""

    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        p = patch.object(serve, 'answered', {})
        p.start()
        self.addCleanup(p.stop)
        self.rows, self.sent = {}, []

    def api(self, path, body=None, timeout=3, method=None):
        self.sent.append((method or ('POST' if body else 'GET'), path, body))
        if path == '/questions/4':
            row = {'id': 9, 'pattern': 'px.example', 'action': body['answer'], 'created_by': body['by'], 'ts': 5}
            self.rows[9] = row
            return {'id': 4, 'answer': body['answer'], 'rules': [row], 'applied': False}
        if path.startswith('/questions/'):
            raise urllib.error.HTTPError(path, 404, 'Not Found', {}, None)
        if path == '/rules':
            return list(self.rows.values())
        if method == 'DELETE':
            return {**self.rows.pop(int(path.rsplit('/', 1)[1])), 'deleted': True, 'applied': True}
        raise AssertionError(path)

    def post(self, path, body):
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_address[1]}{path}',
                                         data=json.dumps(body).encode(), method='POST')
        with patch.object(serve, 'api_request', self.api):
            try:
                with urllib.request.urlopen(request, timeout=3) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

    def test_answer_is_forwarded_and_applied_false_passes_through(self):
        status, reply = self.post('/api/questions/answer', {'id': 4, 'answer': 'block', 'by': 'voice'})
        self.assertEqual((200, False), (status, reply['applied']))
        self.assertEqual([('POST', '/questions/4', {'answer': 'block', 'by': 'dashboard'})], self.sent)

    def test_bad_answers_are_refused_and_nothing_is_forwarded(self):
        for body in ({}, {'id': '4', 'answer': 'block'}, {'id': 4, 'answer': 'delete'}, {'id': True, 'answer': 'block'}):
            self.assertEqual(400, self.post('/api/questions/answer', body)[0], body)
        self.assertEqual([], self.sent)

    def test_already_answered_is_404(self):
        self.assertEqual(404, self.post('/api/questions/answer', {'id': 5, 'answer': 'ignore'})[0])

    def test_undo_deletes_the_answers_rules_once(self):
        self.post('/api/questions/answer', {'id': 4, 'answer': 'block'})
        self.assertEqual((200, {'removed': 1, 'applied': True}), self.post('/api/questions/undo', {'id': 4}))
        self.assertEqual({}, self.rows)
        self.assertEqual(404, self.post('/api/questions/undo', {'id': 4})[0])

    def test_undo_skips_a_rule_whose_id_now_holds_another(self):
        self.post('/api/questions/answer', {'id': 4, 'answer': 'block'})
        self.rows[9] = {**self.rows[9], 'pattern': 'other.example', 'created_by': 'voice'}
        self.assertEqual((200, {'removed': 0, 'applied': None}), self.post('/api/questions/undo', {'id': 4}))
        self.assertIn(9, self.rows)
        self.assertFalse([s for s in self.sent if s[0] == 'DELETE'])
