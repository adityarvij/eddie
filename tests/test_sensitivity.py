"""Sensitivity modes (#110): rule sets from labels, per-device windows, the store handshake and the API."""
import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import DNSRecord

from eddie import api, decision, dns_server, sensitivity, store
from eddie.labels import BROKERS
from tests.test_dns_server import Client, FakeUpstream

TV, PHONE = 'aa:01', 'aa:02'
MIN = 60_000


class RuleSetTests(unittest.TestCase):
    def test_each_mode_gets_its_labelled_sites(self):
        self.assertEqual({'goodrx.com', 'cvs.com', 'walgreens.com', 'teladoc.com', 'betterhelp.com', 'myfitnesspal.com'},
                         set(sensitivity.first_party('health')))
        self.assertIn('chase.com', sensitivity.first_party('money'))
        self.assertIn('target.com', sensitivity.first_party('money'))
        self.assertEqual({'pbskids.org', 'youtubekids.com', 'abcmouse.com', 'roblox.com'},
                         set(sensitivity.first_party('kids')))
        self.assertTrue({'nytimes.com', 'quran.com', 'gop.com'} <= sensitivity.first_party('beliefs'))

    def test_trackers_are_labelled_tracking_and_brokers_never_a_first_party_site(self):
        for mode in sensitivity.MODES:
            sites, trackers = sensitivity.rule_set(mode)
            with self.subTest(mode=mode):
                self.assertFalse(sites & trackers)
                self.assertTrue({'graph.facebook.com', 'app-measurement.com', 'rlcdn.com', 'doubleclick.net'} <= trackers)
                self.assertTrue(set(BROKERS) <= trackers)
                self.assertNotIn('facebook.com', trackers)  # labelled social, not tracking

    def test_describe_counts_and_names_examples(self):
        d = sensitivity.describe('health')
        self.assertEqual(('health', 6, len(sensitivity.trackers())), (d['mode'], d['sites'], d['trackers']))
        self.assertEqual(['BetterHelp', 'CVS', 'GoodRx'], d['examples'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(store.SCHEMA)

    def test_write_turns_modes_on_and_off_and_bumps_the_revision(self):
        self.assertEqual([], sensitivity.wanted(self.conn))
        self.assertEqual(1, sensitivity.write(self.conn, 'money', True))
        self.assertEqual(2, sensitivity.write(self.conn, 'health', True))
        self.assertEqual(['health', 'money'], sensitivity.wanted(self.conn))
        self.assertEqual(3, sensitivity.write(self.conn, 'health', False))
        self.assertEqual(['money'], sensitivity.wanted(self.conn))
        self.assertEqual(0, sensitivity.loaded(self.conn))

    def test_unknown_mode_is_refused_and_nothing_changes(self):
        with self.assertRaises(ValueError):
            sensitivity.write(self.conn, 'sports', True)
        self.assertEqual(0, sensitivity.revision(self.conn))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = decision.Engine()
        self.engine.set_sensitivity(['health'])

    def ask(self, domain, device=TV, at=0):
        return self.engine.evaluate(domain, device, now_ms=at)

    def test_trackers_after_a_health_site_are_blocked_for_that_device_only(self):
        self.assertEqual('flag', self.ask('graph.facebook.com').action)  # no window yet
        self.assertNotEqual('block', self.ask('www.cvs.com', at=1000).action)
        d = self.ask('graph.facebook.com', at=2000)
        self.assertEqual(('block', 'sensitivity'), (d.action, d.source))
        self.assertIn('health protection', d.reason)
        self.assertNotIn('cvs', d.reason)  # the visited site is never logged on the tracker's row
        self.assertEqual('flag', self.ask('graph.facebook.com', PHONE, at=2000).action)

    def test_first_party_site_keeps_working_inside_the_window(self):
        self.ask('cvs.com', at=0)
        for domain in ('cvs.com', 'www.cvs.com', 'goodrx.com'):
            self.assertNotEqual('block', self.ask(domain, at=1000).action, domain)
        self.engine.set_verdicts({'cvs.com': decision.Verdict('tracker', 0.9)})
        self.assertNotEqual('block', self.ask('cvs.com', at=2000).action)

    def test_window_closes_after_window_ms(self):
        self.ask('goodrx.com', at=0)
        self.assertEqual('block', self.ask('rlcdn.com', at=sensitivity.WINDOW_MS - 1).action)
        self.assertEqual('flag', self.ask('rlcdn.com', at=sensitivity.WINDOW_MS + 1).action)

    def test_brokers_and_ai_tracker_verdicts_block_in_the_window(self):
        self.engine.set_verdicts({'pixel.example': decision.Verdict('tracker', 0.8, 'ad pixel')})
        self.ask('teladoc.com', at=0)
        self.assertEqual('block', self.ask(next(iter(BROKERS)), at=1).action)
        self.assertEqual(('block', 'sensitivity'), (self.ask('pixel.example', at=2).action, self.ask('pixel.example', at=2).source))
        self.assertEqual('flag', self.ask('unknown.example', at=3).action)

    def test_other_categories_open_no_window(self):
        self.ask('chase.com', at=0)
        self.assertEqual('flag', self.ask('graph.facebook.com', at=1).action)

    def test_rules_and_allowlist_beat_it_and_an_allowed_site_still_opens_the_window(self):
        self.engine.set_rules([decision.Rule(1, 'graph.facebook.com', 'allow'), decision.Rule(2, 'cvs.com', 'allow')])
        self.engine.set_lists({'app-measurement.com'}, set())
        self.assertEqual(('allow', 'rule'), (self.ask('cvs.com', at=0).action, self.ask('cvs.com', at=0).source))
        self.assertEqual('allow', self.ask('graph.facebook.com', at=1).action)
        self.assertEqual('allow', self.ask('app-measurement.com', at=1).action)
        self.assertEqual('block', self.ask('rlcdn.com', at=1).action)

    def test_turning_the_mode_off_removes_its_rule_set_and_open_windows(self):
        self.ask('cvs.com', at=0)
        self.engine.set_sensitivity([])
        self.assertEqual({}, self.engine.protected)
        self.assertEqual(frozenset(), self.engine.sensitive_trackers)
        self.assertEqual('flag', self.ask('graph.facebook.com', at=1).action)
        self.assertEqual(0, len(self.engine.windows))

    def test_turning_another_mode_off_keeps_this_ones_windows(self):
        self.engine.set_sensitivity(['health', 'money'])
        self.ask('cvs.com', TV, at=0)
        self.ask('chase.com', PHONE, at=0)
        self.engine.set_sensitivity(['health'])
        self.assertEqual('block', self.ask('rlcdn.com', TV, at=1).action)
        self.assertEqual('flag', self.ask('rlcdn.com', PHONE, at=1).action)

    def test_windows_are_bounded(self):
        with patch.object(decision, 'MAX_WINDOWS', 2):
            for i in range(3):
                self.ask('cvs.com', f'dev{i}', at=i)
        self.assertEqual(['dev1', 'dev2'], list(self.engine.windows))

    def test_load_from_store_takes_the_wanted_modes(self):
        conn = sqlite3.connect(':memory:')
        conn.executescript(store.SCHEMA)
        sensitivity.write(conn, 'kids', True)
        engine = decision.Engine()
        engine.load_from_store(conn)
        self.assertEqual({'kids'}, set(engine.protected.values()))
        sensitivity.write(conn, 'kids', False)
        engine.load_from_store(conn)
        self.assertEqual({}, engine.protected)


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class SensitivityAPITests(unittest.IsolatedAsyncioTestCase):
    """POST /sensitivity against a real DNS server reading the same eddie.db."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}),
                  patch.object(decision, 'engine', decision.Engine()),
                  patch.dict(api.db, {'conn': None}),
                  patch.object(api, 'clients', set())):
            p.start()
            self.closers.append(p.stop)
        self.ws = FakeSocket()
        api.clients.add(self.ws)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie')

    async def asyncTearDown(self):
        await self.http.aclose()
        if api.db['conn']:
            api.db['conn'].close()
        for close in reversed(self.closers):
            close()

    async def start_dns(self):
        loop = asyncio.get_running_loop()
        up, _ = await loop.create_datagram_endpoint(FakeUpstream, local_addr=('127.0.0.1', 0))
        server = await dns_server.serve('127.0.0.1', 0, [up.get_extra_info('sockname')], 0.3)
        client, proto = await loop.create_datagram_endpoint(Client, remote_addr=server.get_extra_info('sockname'))
        watcher = dns_server.StoreWatcher(self.path, decision.engine)
        task = asyncio.create_task(dns_server.watch_store(watcher, every=0.02))
        self.closers += [up.close, server.close, client.close, task.cancel, watcher.conn.close]
        self.dns = client, proto
        await asyncio.sleep(0.05)

    async def ask(self, name):
        client, proto = self.dns
        client.sendto(DNSRecord.question(name).pack())
        reply = DNSRecord.parse(await asyncio.wait_for(proto.replies.get(), 2))
        return str(reply.rr[0].rdata)

    async def test_on_is_acknowledged_by_dns_and_blocks_trackers_after_a_health_site(self):
        await self.start_dns()
        r = await self.http.post('/sensitivity', json={'mode': 'health', 'on': True})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual((True, True, ['health']), (body['on'], body['applied'], body['protected']))
        self.assertEqual(sensitivity.describe('health')['trackers'], body['trackers'])
        self.assertNotEqual('0.0.0.0', await self.ask('www.cvs.com'))
        self.assertEqual('0.0.0.0', await self.ask('graph.facebook.com'))
        event = self.ws.sent[-1]
        self.assertEqual(('sensitivity', 'health', True, True), (event['type'], event['mode'], event['on'], event['applied']))

        r = await self.http.post('/sensitivity', json={'mode': 'health', 'on': False})
        self.assertEqual((False, True, []), (r.json()['on'], r.json()['applied'], r.json()['protected']))
        self.assertNotEqual('0.0.0.0', await self.ask('graph.facebook.com'))

    async def test_get_lists_every_mode_and_whether_dns_loaded_it(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.05):
            await self.http.post('/sensitivity', json={'mode': 'money', 'on': True})
        body = (await self.http.get('/sensitivity')).json()
        self.assertEqual((['money'], False), (body['protected'], body['applied']))
        self.assertEqual(['health', 'kids', 'money', 'beliefs'], [m['mode'] for m in body['modes']])
        self.assertEqual([False, False, True, False], [m['on'] for m in body['modes']])

    async def test_without_dns_it_is_stored_but_not_acknowledged(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.05):
            r = await self.http.post('/sensitivity', json={'mode': 'kids', 'on': True})
        self.assertEqual((True, False), (r.json()['on'], r.json()['applied']))

    async def test_bad_requests_change_nothing(self):
        for body in ({'mode': 'sports', 'on': True}, {'mode': 'health'}, {'on': True}, {}):
            self.assertEqual(422, (await self.http.post('/sensitivity', json=body)).status_code, body)
        self.assertEqual([], (await self.http.get('/sensitivity')).json()['protected'])


class WatcherAckTests(unittest.TestCase):
    def test_watcher_records_the_sensitivity_revision_it_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'eddie.db')
            conn = store.connect(path)
            engine = decision.Engine()
            watcher = dns_server.StoreWatcher(path, engine)
            watcher.poll()
            rev = sensitivity.write(conn, 'beliefs', True)
            self.assertTrue(watcher.poll())
            self.assertEqual(rev, sensitivity.loaded(conn))
            self.assertIn('beliefs', engine.protected.values())
            self.assertFalse(watcher.poll())
            watcher.conn.close()
            conn.close()
