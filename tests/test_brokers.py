import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from eddie import api, insights, labels
from eddie.beacons import Beacons, Idle


class BrokerClassification(unittest.TestCase):
    def test_known_brokers_and_exchanges_by_label_suffix(self):
        self.assertEqual({'company': 'LiveRamp', 'kind': 'data broker'}, labels.broker('IDSYNC.RLCDN.COM.'))
        self.assertEqual({'company': 'Magnite', 'kind': 'ad exchange'}, labels.broker('fastlane.rubiconproject.com'))
        self.assertEqual('ad exchange', labels.broker('pubmatic.com')['kind'])

    def test_unknown_and_lookalike_domains_are_not_brokers(self):
        for domain in ('apple.com', 'notrlcdn.com', 'rlcdn.com.evil.example', 'printer', '192.168.1.2', ''):
            self.assertIsNone(labels.broker(domain), domain)

    def test_only_tracker_domains_are_listed(self):
        # Registered brokers' own storefronts are not where household data is sold on.
        for domain in ('gm.com', 't-mobile.com', 'marriott.com'):
            self.assertIsNone(labels.broker(domain), domain)
        self.assertTrue(all(entry['kind'] in ('data broker', 'ad exchange') for entry in labels.BROKERS.values()))


class BrokerCounts(unittest.TestCase):
    now = 100_000_000

    def event(self, device, domain, action='allow', ts=None):
        return {'ts': self.now if ts is None else ts, 'device': device, 'domain': domain, 'action': action}

    def test_counts_per_device_and_company_within_window(self):
        events = [self.event('Fire Stick', 'idsync.rlcdn.com', 'block'), self.event('Fire Stick', 'rlcdn.com'),
                  self.event('Fire Stick', 'fastlane.rubiconproject.com'), self.event('Fire Stick', 'netflix.com'),
                  self.event('iPhone', 'rlcdn.com'), self.event('iPhone', 'pubmatic.com', ts=self.now - 3_600_000),
                  self.event('iPhone', 'pubmatic.com', ts=self.now + 1)]
        counts = insights.broker_counts(events, insights.WINDOWS_MS['1h'], self.now)
        self.assertEqual((counts['requests'], counts['blocked']), (4, 1))
        self.assertEqual(counts['devices'], [{'device': 'Fire Stick', 'requests': 3, 'blocked': 1},
                                             {'device': 'iPhone', 'requests': 1, 'blocked': 0}])
        self.assertEqual(counts['companies'], [
            {'company': 'LiveRamp', 'kind': 'data broker', 'requests': 3, 'blocked': 1},
            {'company': 'Magnite', 'kind': 'ad exchange', 'requests': 1, 'blocked': 0}])
        self.assertEqual(insights.broker_counts(events, insights.WINDOWS_MS['24h'], self.now)['requests'], 5)

    def test_no_broker_traffic_counts_zero(self):
        empty = {'requests': 0, 'blocked': 0, 'devices': [], 'companies': []}
        self.assertEqual(insights.broker_counts([], insights.WINDOWS_MS['1h'], self.now), empty)
        self.assertEqual(insights.broker_counts([self.event('TV', 'netflix.com')], insights.WINDOWS_MS['1h'], self.now), empty)

    def test_summary_and_ingest_expose_brokers(self):
        now = int(time.time() * 1000)
        events = [{'ts': now - 1000, 'device': 'TV', 'domain': 'rlcdn.com', 'action': 'block', 'company': 'LiveRamp', 'category': 'unknown'},
                  {'ts': now - 2 * 3_600_000, 'device': 'TV', 'domain': 'pubmatic.com', 'action': 'allow', 'company': 'PubMatic', 'category': 'unknown'}]
        client = TestClient(api.app, client=('127.0.0.1', 50000))
        with patch.object(api, 'recent', events):
            self.assertEqual(client.get('/summary').json()['brokers']['requests'], 1)
            self.assertEqual(client.get('/summary?window=24h').json()['brokers']['devices'],
                             [{'device': 'TV', 'requests': 2, 'blocked': 1}])
        sent = []
        async def capture(row):
            sent.append(row)
        with patch.object(api, 'recent', []), patch.object(api, 'totals', {'seen': 0, 'blocked': 0}), \
                patch.dict('os.environ', {'EDDIE_DB': 'off'}), \
                patch.object(api, 'timing', {'beacons': Beacons(), 'idle': Idle()}), \
                patch.object(api, 'broadcast', capture):
            for domain in ('rlcdn.com', 'netflix.com'):
                body = {'ts': now, 'device': 'TV', 'device_mac': 'aa:bb:cc:dd:ee:ff', 'domain': domain, 'action': 'allow', 'source': 'default'}
                self.assertEqual(client.post('/ingest', json=body).status_code, 200)
        rows = [row for row in sent if row.get('domain')]
        self.assertEqual([row['broker'] for row in rows], ['data broker', None])


if __name__ == '__main__':
    unittest.main()
