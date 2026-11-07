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
