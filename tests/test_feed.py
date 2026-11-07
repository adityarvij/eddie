import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from eddie import feed
from eddie.events import QueryEvent

from tests import test_dns_server
from tests.test_dns_server import BLOCKED


class Ingest:
    """A stand-in for the API's POST /ingest that records each batch."""

    def __init__(self, delay=0.0, status=200):
        self.batches = []
        self.got = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                time.sleep(delay)
                owner.batches.append((self.path, body))
                owner.got.set()
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def events(self):
        return [e for _, batch in self.batches for e in batch]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def dead_url():
    probe = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
    url = f'http://127.0.0.1:{probe.server_port}'
    probe.server_close()
    return url


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.01)
    return cond()


class FeedPublisherTests(unittest.TestCase):
    def test_event_matches_the_ingest_contract(self):
        event = feed.query_event(1762531200123, '172.20.10.9', BLOCKED, 'A', 'block', 'heuristic', 'keyword "metrics"')
        self.assertEqual(event, QueryEvent(**event).model_dump())
        self.assertEqual(('ip:172.20.10.9', '172.20.10.9'), (event['device_mac'], event['device']))

    def test_every_decision_source_is_accepted_by_ingest(self):
        for source in ('rule', 'allowlist', 'blocklist', 'heuristic', 'ai', 'home_mode', 'default'):
            QueryEvent(**feed.query_event(0, '10.0.0.2', 'a.com', 'A', 'allow', source, ''))

    def test_posts_events_to_ingest(self):
        ingest = Ingest()
        self.addCleanup(ingest.close)
        pub = feed.FeedPublisher(ingest.url)
        pub.publish({'n': 1})
        pub.publish({'n': 2})
        pub.close()
        self.assertEqual({'/ingest'}, {path for path, _ in ingest.batches})
        self.assertEqual([{'n': 1}, {'n': 2}], ingest.events())

    def test_publish_never_blocks_when_the_api_is_slow(self):
        ingest = Ingest(delay=0.5)
        self.addCleanup(ingest.close)
        pub = feed.FeedPublisher(ingest.url, max_pending=5)
        start = time.perf_counter()
        for n in range(50):
            pub.publish({'n': n})
        self.assertLess(time.perf_counter() - start, 0.05)
        self.assertGreater(pub.dropped, 0)
        pub.close()

    def test_api_down_drops_events_and_recovers(self):
        pub = feed.FeedPublisher(dead_url())
        pub.publish({'n': 1})
        self.assertTrue(wait_for(lambda: pub.dropped == 1))
        self.assertTrue(pub.failing)
        ingest = Ingest()
        self.addCleanup(ingest.close)
        pub.url = ingest.url
        pub.publish({'n': 2})
        pub.close()
        self.assertEqual([{'n': 2}], ingest.events())
        self.assertFalse(pub.failing)

    def test_error_status_counts_as_dropped(self):
        ingest = Ingest(status=422)
        self.addCleanup(ingest.close)
        pub = feed.FeedPublisher(ingest.url)
        pub.publish({'n': 1})
        pub.close()
        self.assertEqual(1, pub.dropped)

    def test_refuses_non_local_api(self):
        for url in ('http://8.8.8.8:8000', 'http://api.example.com', 'ftp://127.0.0.1'):
            with self.assertRaises(ValueError):
                feed.FeedPublisher(url)

    def test_env(self):
        with patch.dict('os.environ', {'EDDIE_FEED': 'off'}):
            self.assertIsNone(feed.feed_from_env())
        with patch.dict('os.environ', {'EDDIE_FEED': 'on', 'EDDIE_API_URL': 'http://localhost:9/'}):
            pub = feed.feed_from_env()
            self.assertEqual('http://localhost:9', pub.url)
            pub.close()


class DNSFeedTests(test_dns_server.DNSServerTests):
    """A lookup through the real DNS server lands on /ingest."""

    async def test_every_lookup_is_published(self):
        ingest = Ingest()
        self.addCleanup(ingest.close)
        pub = feed.FeedPublisher(ingest.url)
        conn = await self.start_server([self.upstream_addr], feed=pub)
        await self.ask(conn, 'Google.com')
        await self.ask(conn, BLOCKED, 'AAAA')
        dead = await self.start_server([self.dead_addr], feed=pub)
        await self.ask(dead, 'example.com')  # SERVFAIL still published
        await asyncio.to_thread(wait_for, lambda: len(ingest.events()) == 3)
        pub.close()
        events = ingest.events()
        self.assertEqual([
            ('ip:127.0.0.1', '127.0.0.1', 'google.com', 'A', 'flag', 'default'),
            ('ip:127.0.0.1', '127.0.0.1', BLOCKED, 'AAAA', 'block', 'heuristic'),
            ('ip:127.0.0.1', '127.0.0.1', 'example.com', 'A', 'flag', 'default'),
        ], [(e['device_mac'], e['device'], e['domain'], e['qtype'], e['action'], e['source']) for e in events])
        for e in events:
            QueryEvent(**e)
            self.assertLess(abs(e['ts'] - time.time() * 1000), 60_000)

    async def test_dead_api_does_not_slow_dns(self):
        pub = feed.FeedPublisher(dead_url())
        conn = await self.start_server([self.upstream_addr], feed=pub)
        start = time.perf_counter()
        for _ in range(20):
            await self.ask(conn, BLOCKED)
        self.assertLess((time.perf_counter() - start) / 20, 0.02)
        pub.close()


# DNSFeedTests inherits DNSServerTests' fixtures; don't rerun its tests here.
for name in [n for n in vars(test_dns_server.DNSServerTests) if n.startswith('test_')]:
    setattr(DNSFeedTests, name, None)

if __name__ == '__main__':
    unittest.main()
