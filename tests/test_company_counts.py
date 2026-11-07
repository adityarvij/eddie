import time
import unittest
from unittest.mock import patch
from eddie import insights, labels, api

class OwnershipTests(unittest.TestCase):
    def test_specific_and_parent_labels(self):
        self.assertEqual(('Amazon','telemetry'), labels.lookup('DEVICE-METRICS-US.AMAZON.COM.'))
        self.assertEqual(('Google','ads'), labels.lookup('ads.doubleclick.net'))
        self.assertEqual(('Adobe','unknown'), labels.lookup('sub.demdex.net'))
        self.assertNotEqual('Google', labels.lookup('notdoubleclick.net')[0])
        self.assertNotEqual('Google', labels.lookup('doubleclick.net.evil.example')[0])

    def test_registrable_domains_use_offline_psl(self):
        self.assertEqual(('example.co.uk','unknown'), labels.lookup('a.b.example.co.uk'))
        self.assertEqual(('tenant.github.io','unknown'), labels.lookup('api.tenant.github.io'))
        self.assertEqual(('printer','unknown'), labels.lookup('printer'))
        self.assertEqual(('192.168.1.2','unknown'), labels.lookup('192.168.1.2'))

class CompanyCounts(unittest.TestCase):
    def test_summary_counts_actual_lookups_within_window(self):
        now = 4_000_000
        def event(domain, action='allow', ts=now):
            return {'ts':ts,'domain':domain,'action':action,'device':'TV','company':'untrusted','category':'unknown'}
        events = [event('ads.doubleclick.net','block'), event('google-analytics.com'), event('a.example.co.uk'), event('stale.example',ts=0),event('future.example',ts=now+1)]
        rows = insights.company_counts(events,now)
        self.assertEqual(rows[0],{'company':'Google','requests':2,'blocked':1,'tracking_requests':2})
        self.assertEqual(rows[1]['company'],'example.co.uk')
        self.assertEqual(insights.company_counts([],now),[])
        with patch.object(api, 'recent', events), patch('eddie.insights.time.time',return_value=now/1000):
            self.assertEqual(api.summary()['companies'],rows)

class DeviceCounts(unittest.TestCase):
    def test_counts_per_device_within_window(self):
        now = 100_000_000
        def event(device, domain, action='allow', ts=now):
            return {'ts': ts, 'device': device, 'domain': domain, 'action': action}
        events = [event('Fire Stick', 'device-metrics-us.amazon.com', 'block'),
                  event('Fire Stick', 'ads.doubleclick.net', 'block'), event('Fire Stick', 'unagi-na.amazon.com', 'block'),
                  event('Fire Stick', 'odd.example', 'flag'), event('iPhone', 'apple.com'),
                  event('iPhone', 'old.example', 'block', ts=now - 3_600_000)]
        rows = insights.device_counts(events, insights.WINDOWS_MS['1h'], now)
        self.assertEqual(rows[0], {'device': 'Fire Stick', 'requests': 4, 'blocked': 3, 'flagged': 1,
                                   'blocked_companies': [{'company': 'Amazon', 'blocked': 2}, {'company': 'Google', 'blocked': 1}]})
        self.assertEqual(rows[1], {'device': 'iPhone', 'requests': 1, 'blocked': 0, 'flagged': 0, 'blocked_companies': []})
        self.assertEqual(insights.device_counts(events, insights.WINDOWS_MS['24h'], now)[1]['blocked'], 1)

    def test_summary_window_and_event_filters(self):
        from fastapi.testclient import TestClient
        now = int(time.time() * 1000)
        events = [{'ts': now - 1000, 'device': 'TV', 'domain': 'a.example', 'action': 'block', 'company': 'example', 'category': 'unknown'},
                  {'ts': now - 2 * 3_600_000, 'device': 'TV', 'domain': 'b.example', 'action': 'allow', 'company': 'example', 'category': 'unknown'}]
        with patch.object(api, 'recent', events):
            client = TestClient(api.app, client=('127.0.0.1', 50000))
            self.assertEqual(client.get('/summary').json()['devices'][0]['requests'], 1)
            self.assertEqual(client.get('/summary?window=24h').json()['devices'][0]['requests'], 2)
            self.assertEqual(client.get('/summary?window=1y').status_code, 422)
            self.assertEqual([e['domain'] for e in client.get('/events?action=block').json()], ['a.example'])
            self.assertEqual([e['domain'] for e in client.get('/events?domain=b.example').json()], ['b.example'])
            self.assertEqual(client.get('/events?action=nope').status_code, 422)
