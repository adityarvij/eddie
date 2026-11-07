"""Traffic API access (#96): writes off loopback need the token; /ws/events checks Origin."""
import os
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from eddie import api, classifier

LOCAL = ('127.0.0.1', 50000)
IPAD = ('192.168.1.20', 50000)
TOKEN = 'pair-me-please'
MODE = {'home_mode': True}


class WriteAuthTests(unittest.TestCase):
    def setUp(self):
        for p in (patch.dict(os.environ, {'EDDIE_API_TOKEN': TOKEN, 'EDDIE_DB': 'off'}),
                  patch.dict(api.state, {'home_mode': False}),
                  patch.object(api, 'clients', set())):
            p.start()
            self.addCleanup(p.stop)

    def post(self, client=IPAD, headers=None):
        return TestClient(api.app, client=client).post('/mode', json=MODE, headers=headers or {})

    def test_loopback_writes_without_a_token(self):
        with patch.dict(os.environ, {'EDDIE_API_TOKEN': ''}):
            self.assertEqual(self.post(LOCAL).status_code, 200)
        self.assertEqual(self.post(('::1', 50000)).status_code, 200)
        self.assertEqual(self.post(('::ffff:127.0.0.1', 50000)).status_code, 200)

    def test_another_host_needs_the_token(self):
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.post(headers={'Authorization': 'Bearer wrong'}).status_code, 401)
        self.assertEqual(self.post(headers={'Authorization': TOKEN}).status_code, 401)
        self.assertFalse(api.state['home_mode'])
        self.assertEqual(self.post(headers={'Authorization': f'Bearer {TOKEN}'}).status_code, 200)
        self.assertTrue(api.state['home_mode'])

    def test_no_token_configured_refuses_every_other_host(self):
        with patch.dict(os.environ, {'EDDIE_API_TOKEN': ''}):
            self.assertEqual(self.post(headers={'Authorization': 'Bearer '}).status_code, 401)

    def test_every_write_endpoint_is_guarded(self):
        client = TestClient(api.app, client=IPAD)
        for method, path in (('post', '/ingest'), ('post', '/ai/verdict'), ('post', '/mode'),
                             ('post', '/offline-mode'), ('post', '/rules'), ('delete', '/rules/1'),
                             ('put', '/devices/aa:bb:cc:dd:ee:ff')):
            kwargs = {} if method == 'delete' else {'json': {}}
            self.assertEqual(getattr(client, method)(path, **kwargs).status_code, 401, path)

    def test_reads_stay_open(self):
        self.assertEqual(TestClient(api.app, client=IPAD).get('/mode').status_code, 200)

    def test_foreign_origin_cannot_write_even_from_loopback(self):
        for origin in ('http://evil.example', 'null', 'http://evil.example:8000'):
            response = self.post(LOCAL, {'Origin': origin, 'Host': 'evil.example:8000'})
            self.assertEqual(response.status_code, 403, origin)
        self.assertFalse(api.state['home_mode'])
        self.assertEqual(self.post(LOCAL, {'Origin': 'http://localhost:5173'}).status_code, 200)

    def test_clients_send_the_token(self):
        self.assertEqual(classifier.auth_headers(), {'Authorization': f'Bearer {TOKEN}'})
        with patch.dict(os.environ, {'EDDIE_API_TOKEN': ''}):
            self.assertEqual(classifier.auth_headers(), {})


class WebSocketOriginTests(unittest.TestCase):
    def connect(self, origin, host='localhost:8000'):
        headers = {'host': host, **({'origin': origin} if origin else {})}
        with TestClient(api.app, client=LOCAL).websocket_connect('/ws/events', headers=headers) as ws:
            return ws.receive_json()['type']

    def test_eddies_own_pages_and_non_browsers_connect(self):
        self.assertEqual(self.connect(None), 'snapshot')
        self.assertEqual(self.connect('http://localhost:5173'), 'snapshot')
        self.assertEqual(self.connect('http://127.0.0.1:5173'), 'snapshot')
        self.assertEqual(self.connect('http://192.168.1.5:5173', '192.168.1.5:8000'), 'snapshot')
        self.assertEqual(self.connect('http://eddie.local:5173', 'eddie.local:8000'), 'snapshot')
        self.assertEqual(self.connect('http://[::1]:5173', '[::1]:8000'), 'snapshot')

    def test_foreign_origins_are_refused(self):
        for origin, host in (('https://evil.example', 'localhost:8000'),
                             ('http://192.168.1.9:5173', '192.168.1.5:8000'),
                             ('null', 'localhost:8000'),
                             # DNS rebinding: the page and Host agree, but name a domain, not this machine.
                             ('http://evil.example:8000', 'evil.example:8000')):
            with self.assertRaises(WebSocketDisconnect, msg=origin):
                self.connect(origin, host)


if __name__ == '__main__':
    unittest.main()
