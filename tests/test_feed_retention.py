"""The live feed keeps no more history than eddie.db (#129)."""
import time
import unittest
from collections import deque
from unittest.mock import patch

from fastapi.testclient import TestClient

from eddie import api

DAY = 86_400_000


def event(domain, ts):
    return {'ts': ts, 'device': 'TV', 'domain': domain, 'action': 'allow', 'company': 'example', 'category': 'unknown'}


class FeedRetention(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.feed = deque([event('old.example', self.now - 8 * DAY), event('edge.example', self.now - 7 * DAY + 60_000),
                           event('new.example', self.now - 1000)], maxlen=20000)
        patcher = patch.object(api, 'recent', self.feed)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(api.app)

    def domains(self):
        return [e['domain'] for e in self.feed]

    def test_default_window_matches_query_log(self):
        self.assertEqual(api.FEED_RETENTION_MS, 7 * DAY)

    def test_reads_drop_expired_events_while_idle(self):
        self.assertEqual([e['domain'] for e in self.client.get('/events').json()], ['edge.example', 'new.example'])
        self.assertEqual(self.domains(), ['edge.example', 'new.example'])

    def test_summary_and_report_fallback_drop_expired_events(self):
        self.client.get('/summary')
        self.assertNotIn('old.example', self.domains())
        self.feed.appendleft(event('old.example', self.now - 8 * DAY))
        with patch.dict('os.environ', {'EDDIE_DB': 'off'}):
            events, source = api.report_events(168)
        self.assertEqual(source, 'feed')
        self.assertNotIn('old.example', [e['domain'] for e in events])

    def test_ingest_trims_and_never_keeps_expired_events(self):
        body = [{'ts': self.now - 9 * DAY, 'device_mac': 'aa', 'device': 'TV', 'domain': 'late.example', 'action': 'allow', 'source': 'blocklist'},
                {'ts': self.now, 'device_mac': 'aa', 'device': 'TV', 'domain': 'fresh.example', 'action': 'allow', 'source': 'blocklist'}]
        seen = api.totals['seen']
        self.assertEqual(self.client.post('/ingest', json=body).status_code, 200)
        self.assertEqual(self.domains(), ['edge.example', 'new.example', 'fresh.example'])
        self.assertEqual(api.totals['seen'], seen + 2)

    def test_window_follows_the_clock(self):
        with patch('eddie.api.time.time', return_value=(self.now + 60_000 + 1) / 1000):
            self.client.get('/events')
        self.assertEqual(self.domains(), ['new.example'])

    def test_websocket_snapshot_has_no_expired_events(self):
        with self.client.websocket_connect('/ws/events') as ws:
            snapshot = ws.receive_json()
        self.assertEqual([e['domain'] for e in snapshot['events']], ['edge.example', 'new.example'])


if __name__ == '__main__':
    unittest.main()
