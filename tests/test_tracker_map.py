"""Cross-device tracker map (#107): the aggregation, its API and the dashboard bridge relay."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx

from eddie import api, insights, labels, rules

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'frontend'))
import serve

NOW = 100_000_000
HOUR = insights.WINDOWS_MS['1h']


def event(device, domain, action='allow', ts=NOW):
    return {'ts': ts, 'device': device, 'domain': domain, 'action': action}


EVENTS = [event('TV', 'ads.doubleclick.net', 'block'), event('TV', 'google-analytics.com'),
          event('Phone', 'ads.doubleclick.net'), event('Tablet', 'www.google-analytics.com'),
          event('TV', 'match.rlcdn.com'), event('Phone', 'idsync.rlcdn.com', 'block'),
          event('Laptop', 'example.org'),  # a device with no tracking lookups still counts
          event('Phone', 'apple.com'),  # not tracking: no Apple row
          event('Fridge', 'ads.doubleclick.net', ts=NOW - HOUR)]  # outside the hour


class Aggregation(unittest.TestCase):
    def test_companies_with_their_devices_most_devices_first(self):
        result = insights.tracker_map(EVENTS, HOUR, NOW)
        self.assertEqual(4, result['device_count'])
        self.assertEqual(['Laptop', 'Phone', 'TV', 'Tablet'], result['devices'])
        google, liveramp = result['companies']
        self.assertEqual({'company': 'Google', 'requests': 4, 'blocked': 1, 'device_count': 3,
                          'devices': [{'device': 'TV', 'requests': 2}, {'device': 'Phone', 'requests': 1},
                                      {'device': 'Tablet', 'requests': 1}], 'id_sync': []}, google)
        self.assertEqual({'company': 'LiveRamp', 'requests': 2, 'blocked': 1, 'device_count': 2,
                          'devices': [{'device': 'Phone', 'requests': 1}, {'device': 'TV', 'requests': 1}],
                          'id_sync': [{'domain': 'rlcdn.com', 'requests': 2, 'blocked': 1,
                                       'devices': ['Phone', 'TV']}]}, liveramp)
        self.assertEqual('Google sees 3 of your 4 devices.', result['text'])

    def test_window_and_empty(self):
        self.assertEqual(5, insights.tracker_map(EVENTS, insights.WINDOWS_MS['24h'], NOW)['device_count'])
        empty = insights.tracker_map([], HOUR, NOW)
        self.assertEqual({'device_count': 0, 'devices': [], 'companies': [],
                          'text': 'No tracking lookups in this window.'}, empty)
        self.assertEqual('LiveRamp sees 1 of your 1 device.',
                         insights.tracker_map([event('TV', 'rlcdn.com')], HOUR, NOW)['text'])

    def test_id_sync_matches_label_boundaries_only(self):
        self.assertEqual('rlcdn.com', labels.id_sync('A.RLCDN.COM.'))
        self.assertEqual('tapad.com', labels.id_sync('tapad.com'))
        self.assertIsNone(labels.id_sync('notrlcdn.com'))
        self.assertIsNone(labels.id_sync('rlcdn.com.evil.example'))
        self.assertIsNone(labels.id_sync('ads.doubleclick.net'))


class TrackerMapAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': os.path.join(self.tmp.name, 'eddie.db')}),
                  patch.dict(api.db, {'conn': None}), patch.object(api, 'clients', set()),
                  patch.object(api, 'APPLY_TIMEOUT', 0.05),  # no DNS server here: never applied
                  patch.object(api, 'recent', list(EVENTS)),
                  patch.object(api, 'feed', lambda: api.recent),
                  patch('eddie.insights.time.time', return_value=NOW / 1000)):
            p.start()
            self.closers.append(p.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie')

    async def asyncTearDown(self):
        await self.http.aclose()
        if api.db['conn']:
            api.db['conn'].close()
        for close in reversed(self.closers):
            close()

    async def sync_entry(self):
        r = await self.http.get('/tracker-map')
        self.assertEqual(200, r.status_code)
        self.assertEqual('24h', r.json()['window'])
        return next(c for c in r.json()['companies'] if c['company'] == 'LiveRamp')['id_sync'][0]

    async def test_block_writes_one_rule_and_does_not_claim_dns(self):
        self.assertIsNone((await self.sync_entry())['rule_id'])
        r = await self.http.post('/tracker-map/block', json={'domain': 'RLCDN.com.'})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual(('*.rlcdn.com', 'block', None, 'dashboard', False, False),
                         (body['pattern'], body['action'], body['device_mac'], body['created_by'],
                          body['existing'], body['applied']))
        self.assertEqual(body['id'], (await self.sync_entry())['rule_id'])
        again = (await self.http.post('/tracker-map/block', json={'domain': 'rlcdn.com'})).json()
        self.assertEqual((True, None, body['id']), (again['existing'], again['applied'], again['id']))
        self.assertEqual(1, len(rules.all_rules(api.rules_db())))

    async def test_only_id_sync_domains_are_blocked(self):
        for domain in ('doubleclick.net', 'match.rlcdn.com', 'evil.example', ''):
            r = await self.http.post('/tracker-map/block', json={'domain': domain})
            self.assertEqual(422, r.status_code, domain)
        self.assertEqual([], rules.all_rules(api.rules_db()))

    async def test_map_shows_without_a_database(self):
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            self.assertIsNone((await self.sync_entry())['rule_id'])
        self.assertEqual(422, (await self.http.get('/tracker-map?window=7d')).status_code)


class Bridge(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sent = []
        self.answer = lambda path, body: {'pattern': '*.rlcdn.com', 'applied': True, 'existing': False}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, body=None):
        def record(api_path, payload=None, timeout=3, method=None):
            self.sent.append((api_path, payload))
            return self.answer(api_path, payload)
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{self.server.server_address[1]}{path}', data=data)
        with patch.object(serve, 'api_request', record):
            try:
                with urllib.request.urlopen(request, timeout=3) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

    def test_relays_the_map_read_only(self):
        self.answer = lambda path, body: {'companies': []}
        self.assertEqual((200, {'companies': []}), self.call('/api/tracker-map'))
        self.assertEqual([('/tracker-map?window=24h', None)], self.sent)

    def test_block_is_forwarded_and_refusals_explained(self):
        self.assertEqual(200, self.call('/api/tracker-map/block', {'domain': 'rlcdn.com'})[0])
        self.assertEqual([('/tracker-map/block', {'domain': 'rlcdn.com'})], self.sent)
        for body in ({}, {'domain': 3}, {'domain': ''}, {'domain': 'x' * 254}):
            self.assertEqual(400, self.call('/api/tracker-map/block', body)[0], body)
        self.assertEqual(1, len(self.sent))

        def refuse(code):
            def answer(path, body):
                raise urllib.error.HTTPError(path, code, 'no', {}, None)
            return answer
        self.answer = refuse(422)
        self.assertEqual(422, self.call('/api/tracker-map/block', {'domain': 'doubleclick.net'})[0])
        self.answer = refuse(500)
        self.assertEqual(502, self.call('/api/tracker-map/block', {'domain': 'rlcdn.com'})[0])

    def test_a_paired_device_may_block(self):
        self.assertIn('/api/tracker-map/block', serve.PAIRED_CONTROLS)


if __name__ == '__main__':
    unittest.main()
