import os
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from eddie import api, baselines, store

TV = 'f0:4f:7c:11:22:33'
MIN = 60_000
HOUR = 60 * MIN


def ev(ts, domain, mac=TV, action='allow'):
    return {'ts': ts, 'device_mac': mac, 'device': 'Fire Stick', 'domain': domain, 'action': action,
            'source': 'default'}


class BaselineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.connect(os.path.join(tmp.name, 'eddie.db'))
        self.addCleanup(self.conn.close)
        self.t0 = 1_762_500_000_000

    def feed(self, *events, lookups=10, learn_ms=HOUR):
        out = []
        with self.conn:
            for e in events:
                out += baselines.observe(self.conn, e, lookups, learn_ms)
        return out

    def learn(self, domains=('netflix.com', 'amazon.com'), n=10):
        """n lookups spread over an hour, cycling through domains: the baseline is active after."""
        return self.feed(*[ev(self.t0 + i * HOUR // (n - 1), domains[i % len(domains)]) for i in range(n)])

    def state(self, key=TV, now=None):
        return baselines.state(self.conn, key, now or self.t0, 10, HOUR)

    def test_learning_needs_both_the_lookups_and_the_hour(self):
        # 10 lookups in a minute: enough lookups, not enough time.
        self.assertEqual([], self.feed(*[ev(self.t0 + i * 1000, 'netflix.com') for i in range(10)]))
        self.assertEqual('learning', self.state()['state'])
        # An hour later, but the second lookup only.
        tv2 = 'aa:bb:cc:dd:ee:ff'
        self.feed(ev(self.t0, 'netflix.com', tv2), ev(self.t0 + 2 * HOUR, 'netflix.com', tv2))
        self.assertEqual('learning', self.state(tv2)['state'])
        self.assertEqual(8, self.state(tv2)['lookups_left'])
        # The 10th lookup after the hour ends learning.
        out = self.feed(ev(self.t0 + HOUR, 'netflix.com'))
        self.assertEqual([('baseline_state', 'active')], [(e['type'], e['state']) for e in out])
        self.assertEqual('active', self.state()['state'])

    def test_no_new_domain_events_while_learning(self):
        out = self.feed(*[ev(self.t0 + i, f'site{i}.example') for i in range(9)])
        self.assertEqual([], out)
        self.assertEqual(9, self.state()['learned_domains'])

    def test_new_registrable_domain_after_learning_fires_once(self):
        self.learn()
        later = self.t0 + 2 * HOUR
        out = self.feed(ev(later, 'api.anthropic.com'), ev(later + 1, 'claude.anthropic.com'),
                        ev(later + 2, 'cdn.netflix.com'), ev(later + 3, 'api.anthropic.com'))
        self.assertEqual([{'type': 'new_domain', 'ts': later, 'device_mac': TV, 'device': 'Fire Stick',
                           'domain': 'anthropic.com', 'query': 'api.anthropic.com', 'action': 'allow'}], out)
        self.assertEqual([], self.feed(ev(later + 10 * HOUR, 'www.anthropic.com')))
        self.assertEqual(['anthropic.com'], [d['domain'] for d in self.state()['new_domains']])

    def test_baselines_are_per_device_and_ip_keys_match_devices(self):
        self.learn()
        phone = 'ip:172.20.10.3'
        self.feed(ev(self.t0, 'anthropic.com', phone))
        self.assertIsNotNone(self.state('172.20.10.3'))
        out = self.feed(ev(self.t0 + 2 * HOUR, 'anthropic.com'))
        self.assertEqual(['new_domain'], [e['type'] for e in out])  # the phone knowing it doesn't count for the TV

    def test_blocked_lookups_count_as_contact(self):
        self.learn()
        out = self.feed(ev(self.t0 + 2 * HOUR, 'tracker.example', action='block'))
        self.assertEqual('block', out[0]['action'])

    def test_too_many_new_domains_makes_the_baseline_unstable(self):
        self.learn()
        later = self.t0 + 2 * HOUR
        out = self.feed(*[ev(later + i, f'site{i}.example') for i in range(baselines.UNSTABLE_NEW + 5)])
        self.assertEqual(['new_domain'] * baselines.UNSTABLE_NEW + ['baseline_state'], [e['type'] for e in out])
        self.assertEqual('unstable', out[-1]['state'])
        self.assertEqual('unstable', self.state()['state'])
        self.assertEqual(baselines.UNSTABLE_NEW + 5, self.state()['new_domain_count'])  # still recorded

    def test_new_domains_spread_out_stay_active(self):
        self.learn()
        out = self.feed(*[ev(self.t0 + 2 * HOUR + i * 2 * HOUR, f'site{i}.example')
                          for i in range(baselines.UNSTABLE_NEW + 5)])
        self.assertEqual(['new_domain'] * (baselines.UNSTABLE_NEW + 5), [e['type'] for e in out])

    def test_reset_starts_learning_again(self):
        self.learn()
        self.feed(ev(self.t0 + 2 * HOUR, 'anthropic.com'))
        self.assertEqual(3, baselines.reset(self.conn, TV))
        self.assertIsNone(self.state())
        self.assertIsNone(baselines.reset(self.conn, TV))
        self.assertEqual([], self.feed(ev(self.t0 + 3 * HOUR, 'brand-new.example')))
        self.assertEqual('learning', self.state()['state'])
        self.assertEqual(1, self.state()['lookups'])

    def test_state_reports_what_is_left_to_learn(self):
        self.feed(ev(self.t0, 'netflix.com'), ev(self.t0 + 1, 'amazon.com'))
        st = self.state(now=self.t0 + 15 * MIN)
        self.assertEqual(('learning', 2, 8, 45 * MIN, 2), (st['state'], st['lookups'], st['lookups_left'],
                                                            st['ms_left'], st['domains']))

    def test_retention_forgets_unused_domains_and_idle_devices(self):
        self.learn()
        last = self.t0 + HOUR
        self.feed(ev(last + 6 * 24 * HOUR, 'netflix.com'))  # still in use: kept
        store.prune(self.conn, 7, now_ms=last + 7 * 24 * HOUR + 1)
        self.assertEqual(['netflix.com'], [d for (d,) in self.conn.execute('SELECT domain FROM baseline_domains')])
        out = self.feed(ev(last + 7 * 24 * HOUR + 2, 'amazon.com'))
        self.assertEqual(['new_domain'], [e['type'] for e in out])  # forgotten, so new again
        store.prune(self.conn, 7, now_ms=last + 30 * 24 * HOUR)
        self.assertIsNone(self.state())


class BaselineAPITests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sent = []

        async def broadcast(msg):
            self.sent.append(msg)

        for p in (patch.dict(os.environ, {'EDDIE_DB': os.path.join(tmp.name, 'eddie.db')}),
                  patch.dict(api.db, {'conn': None}), patch.object(api, 'recent', []),
                  patch.object(api, 'totals', {'seen': 0, 'blocked': 0}), patch.object(api, 'broadcast', broadcast),
                  patch.object(api, 'baseline_settings', (5, HOUR))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: api.db['conn'] and api.db['conn'].close())
        self.client = TestClient(api.app, client=('127.0.0.1', 50000))
        self.now = int(time.time() * 1000)

    def ingest(self, *events):
        self.assertEqual(200, self.client.post('/ingest', json=list(events)).status_code)

    def test_ingest_learns_then_pushes_new_domain_and_api_shows_state(self):
        start = self.now - 2 * HOUR
        self.ingest(*[ev(start + i * MIN, 'netflix.com') for i in range(4)])
        self.assertEqual('learning', self.client.get(f'/devices/{TV}/baseline').json()['state'])
        self.ingest(ev(self.now - MIN, 'netflix.com'), ev(self.now, 'api.anthropic.com'))
        kinds = [(m['type'], m.get('state') or m.get('domain')) for m in self.sent if m['type'] != 'query']
        self.assertIn(('baseline_state', 'active'), kinds)
        self.assertIn(('new_domain', 'anthropic.com'), kinds)
        self.assertEqual([('new_domain', 'anthropic.com'), ('totals', None)], kinds[-2:])  # after the queries
        body = self.client.get(f'/devices/{TV.upper()}/baseline').json()
        self.assertEqual(('active', 2, TV), (body['state'], body['domains'], body['device']))
        self.assertEqual([TV], [b['device_mac'] for b in self.client.get('/baselines').json()])

    def test_reset_and_missing_baselines(self):
        self.assertEqual(404, self.client.get(f'/devices/{TV}/baseline').status_code)
        self.assertEqual(404, self.client.delete(f'/devices/{TV}/baseline').status_code)
        self.assertEqual(422, self.client.get('/devices/bad key!/baseline').status_code)
        self.ingest(ev(self.now, 'netflix.com'))
        r = self.client.delete(f'/devices/{TV}/baseline')
        self.assertEqual({'device_mac': TV, 'reset': True, 'domains_deleted': 1}, r.json())
        self.assertEqual('reset', self.sent[-1]['state'])
        self.assertEqual(404, self.client.get(f'/devices/{TV}/baseline').status_code)

    def test_reset_needs_the_token_off_loopback(self):
        remote = TestClient(api.app, client=('172.20.10.7', 50000))
        self.assertEqual(401, remote.delete(f'/devices/{TV}/baseline').status_code)

    def test_events_past_retention_are_not_learned(self):
        self.ingest(ev(self.now - 30 * 24 * HOUR, 'netflix.com'))
        self.assertEqual(404, self.client.get(f'/devices/{TV}/baseline').status_code)

    def test_db_off_skips_baselines_and_the_api_says_so(self):
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            self.ingest(ev(self.now, 'netflix.com'))
            self.assertEqual(503, self.client.get(f'/devices/{TV}/baseline').status_code)
        self.assertEqual([], [m for m in self.sent if m['type'] in ('new_domain', 'baseline_state')])


if __name__ == '__main__':
    unittest.main()
