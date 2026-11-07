import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve


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

    def call(self, host, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f'http://{host}:{self.port}{path}', data=data, method='POST' if data else 'GET')
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

    def test_lan_cannot_use_controls(self):
        for path in ('/api/voice/start', '/api/voice/stop', '/api/voice/ask', '/api/events'):
            status, body = self.call(lan_ip(), path, {'text': 'hi', 'domain': 'x.com'})
            self.assertEqual(403, status, path)
            self.assertIn('Eddie computer', json.loads(body)['message'])
        self.assertFalse(serve.stopped.is_set())

    def test_localhost_keeps_controls(self):
        status, _ = self.call('127.0.0.1', '/api/events', {'domain': 'example.com', 'action': 'block'})
        self.assertEqual(200, status)


if __name__ == '__main__':
    unittest.main()
