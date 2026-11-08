"""Away mode (#109): occupancy leak ranking, security exemptions, the engine step and /mode."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import DNSRecord

from eddie import api, away, decision, dns_server, store
from tests.test_dns_server import REAL_IP, Client, FakeUpstream, watch
from tests.test_rules_api import FakeSocket

HOUR = away.HOUR_MS
NOW = 1_800_000_000_000
TELEMETRY = 'unagi-na.amazon.com'  # labelled telemetry; no list or keyword blocks it
TV, PLUG, CAM, DOORBELL, LAPTOP = 'aa:00:00:00:00:01', 'aa:00:00:00:00:02', 'aa:00:00:00:00:03', 'aa:00:00:00:00:04', 'aa:00:00:00:00:05'


def log(conn, device, domain, hour, count=1):
    conn.executemany("INSERT INTO queries (ts, device_mac, domain, qtype, action, source, reason)"
                     " VALUES (?, ?, ?, 'A', 'allow', 'default', '')",
                     [(NOW - hour * HOUR - i, device, domain) for i in range(count)])
    conn.commit()


def device(conn, mac, label=None, vendor=None):
    conn.execute("INSERT INTO devices (mac, label, vendor) VALUES (?, ?, ?)", (mac, label, vendor))
    conn.commit()


class RankTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(os.path.join(self.tmp.name, 'eddie.db'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_media_and_burst_hours_rank_devices_and_steady_ones_score_zero(self):
        device(self.conn, TV, 'TV')
        device(self.conn, LAPTOP, 'Laptop')
        for hour in (1, 2, 30):
            log(self.conn, TV, 'nflxvideo.net', hour)
        for hour in range(1, 20):  # a steady beacon: never a burst
            log(self.conn, PLUG, 'tuya.example', hour, 5)
            log(self.conn, LAPTOP, 'news.example', hour, 2)
        log(self.conn, LAPTOP, 'news.example', 3, 40)  # someone browsing: 20x its usual hour
        log(self.conn, TV, 'nflxvideo.net', 24 * 8)  # older than a week: not counted
        covered, exempt = away.rank(self.conn, NOW)
        self.assertEqual([('TV', 3), ('Laptop', 1)], [(d['name'], d['score']) for d in covered])
        self.assertEqual(TV, covered[0]['key'])
        self.assertEqual([], exempt)

    def test_security_devices_are_exempt_by_vendor_name_or_domain(self):
        device(self.conn, CAM, vendor='Ring')
        device(self.conn, DOORBELL, 'Front doorbell')
        device(self.conn, TV, 'TV')
        for mac in (CAM, DOORBELL, TV, LAPTOP):
            log(self.conn, mac, 'youtube.com', 1)
        log(self.conn, LAPTOP, 'ring.com', 2)  # talks to a camera service
        covered, exempt = away.rank(self.conn, NOW)
        self.assertEqual(['TV'], [d['name'] for d in covered])
        self.assertEqual({CAM, DOORBELL, LAPTOP}, {d['key'] for d in exempt})

    def test_security_words_match_whole_words_only(self):
        self.assertTrue(away.is_security('Garage cam', None, None, set()))
        self.assertTrue(away.is_security(None, 'alarm-panel', None, set()))
        self.assertFalse(away.is_security('Camille’s iPad', 'during-tv', 'Apple', {'streaming'}))

    def test_plumbing_lookups_dont_count(self):
        for hour in range(1, 5):
            log(self.conn, TV, '1.0.168.192.in-addr.arpa', hour, 100)
        self.assertEqual(([], []), away.rank(self.conn, NOW))

    def test_write_stores_covered_and_exempt_and_off_clears_them(self):
        device(self.conn, TV, 'TV')
        device(self.conn, CAM, 'Porch cam')
        rev = away.write(self.conn, True, NOW + HOUR, [{'key': TV, 'name': 'TV', 'score': 4}],
                         [{'key': CAM, 'name': 'Porch cam'}])
        self.assertEqual((True, NOW + HOUR, rev), (away.wanted(self.conn), away.until(self.conn), away.revision(self.conn)))
        self.assertEqual(frozenset({TV}), away.covered_keys(self.conn))
        self.assertEqual(([{'key': TV, 'name': 'TV', 'score': 4}], [{'key': CAM, 'name': 'Porch cam'}]),
                         away.stored(self.conn))
        self.assertTrue(away.active(self.conn, NOW))
        self.assertFalse(away.active(self.conn, NOW + HOUR))
        self.assertEqual(rev + 1, away.write(self.conn, False, NOW + HOUR))
        self.assertEqual((False, 0, ([], [])), (away.wanted(self.conn), away.until(self.conn), away.stored(self.conn)))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = decision.Engine()
        self.engine.set_lists(['amazon.com'], [])
        self.engine.set_verdicts({'pixel.tracker.example': decision.Verdict('tracker', 0.9, 'ad network')})
        self.engine.set_away({TV}, NOW + HOUR)

    def test_covered_device_telemetry_is_blocked_others_flagged(self):
        engine = decision.Engine()
        engine.set_away({TV}, NOW + HOUR)
        self.assertEqual(('block', 'away_mode', f'away mode: {TELEMETRY} telemetry'),
                         tuple(vars(engine.evaluate(TELEMETRY, TV, now_ms=NOW)).values()))
        self.assertEqual('block', engine.evaluate('graph.facebook.com', TV, now_ms=NOW).action)
        self.assertEqual('flag', engine.evaluate(TELEMETRY, PLUG, now_ms=NOW).action)
        self.assertEqual('flag', engine.evaluate('news.example', TV, now_ms=NOW).action)

    def test_device_matches_by_client_ip_too(self):
        engine = decision.Engine()
        engine.set_away({'192.168.1.20'})
        self.assertEqual('away_mode', engine.evaluate(TELEMETRY, TV, '192.168.1.20', NOW).source)

    def test_allowlist_and_rules_beat_away(self):
        self.assertEqual(('allow', 'allowlist'), (self.engine.evaluate(TELEMETRY, TV, now_ms=NOW).action,
                                                  self.engine.evaluate(TELEMETRY, TV, now_ms=NOW).source))
        engine = decision.Engine()
        engine.set_away({TV})
        engine.set_rules([decision.Rule(1, TELEMETRY, 'allow', TV)])
        self.assertEqual(('allow', 'rule'), (engine.evaluate(TELEMETRY, TV, now_ms=NOW).action,
                                             engine.evaluate(TELEMETRY, TV, now_ms=NOW).source))

    def test_tracker_verdicts_and_beacons_block_instead_of_flag(self):
        self.assertEqual(('block', 'away mode: likely tracker: ad network'),
                         (self.engine.evaluate('pixel.tracker.example', TV, now_ms=NOW).action,
                          self.engine.evaluate('pixel.tracker.example', TV, now_ms=NOW).reason))
        self.assertEqual('flag', self.engine.evaluate('pixel.tracker.example', PLUG, now_ms=NOW).action)
        results = [self.engine.evaluate('ping.example', TV, now_ms=NOW - 10 * 60_000 + i * 60_000) for i in range(7)]
        self.assertEqual(('block', 'away_mode'), (results[-1].action, results[-1].source))
        self.assertIn('every 60 s', results[-1].reason)

    def test_end_time_stops_blocking_without_a_reload(self):
        engine = decision.Engine()
        engine.set_away({TV}, NOW + HOUR)
        self.assertEqual('block', engine.evaluate(TELEMETRY, TV, now_ms=NOW + HOUR - 1).action)
        self.assertEqual('flag', engine.evaluate(TELEMETRY, TV, now_ms=NOW + HOUR).action)

    def test_load_from_store_takes_only_covered_devices_and_only_while_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(os.path.join(tmp, 'eddie.db'))
            engine = decision.Engine()
            away.write(conn, True, None, [{'key': TV, 'score': 2}], [{'key': CAM}])
            engine.load_from_store(conn)
            self.assertEqual(decision.Away(frozenset({TV}), 0), engine.away)
            away.write(conn, False)
            engine.load_from_store(conn)
            self.assertEqual(decision.Away(), engine.away)
            conn.close()


class AwayAPITests(unittest.IsolatedAsyncioTestCase):
    """POST /mode {away} against a real DNS server, both reading one eddie.db."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}),
                  patch.object(decision, 'engine', decision.Engine()),
                  patch.dict(api.db, {'conn': None}),
                  patch.dict(api.state, {'home_mode': False}),
                  patch.object(api, 'clients', set())):
            p.start()
            self.closers.append(p.stop)
        self.ws = FakeSocket()
        api.clients.add(self.ws)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie')
        conn = store.connect(self.path)
        now = int(time.time() * 1000)
        device(conn, CAM, 'Porch cam')
        for hour in (1, 2):  # this machine's lookups (127.0.0.1) streamed twice this week
            conn.execute("INSERT INTO queries (ts, device_mac, domain) VALUES (?, '127.0.0.1', 'netflix.com')",
                         (now - hour * HOUR,))
        conn.execute("INSERT INTO queries (ts, device_mac, domain) VALUES (?, ?, 'youtube.com')", (now - HOUR, CAM))
        conn.commit()
        conn.close()

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
        stop_watching = watch(watcher, 0.02)
        self.closers += [up.close, server.close, client.close, stop_watching]
        self.dns = client, proto
        await asyncio.sleep(0.05)

    async def ask(self, name):
        client, proto = self.dns
        client.sendto(DNSRecord.question(name).pack())
        return str(DNSRecord.parse(await asyncio.wait_for(proto.replies.get(), 2)).rr[0].rdata)

    def away_events(self):
        return [m for m in self.ws.sent if m['type'] == 'away_mode']

    async def test_on_then_off_is_acknowledged_by_dns_and_spares_security_devices(self):
        await self.start_dns()
        self.assertEqual(REAL_IP, await self.ask(TELEMETRY))
        until = int(time.time() * 1000) + 3 * HOUR
        r = await self.http.post('/mode', json={'away': True, 'away_until': until})
        self.assertEqual(200, r.status_code)
        body = r.json()
        self.assertEqual((True, until, True), (body['away'], body['away_until'], body['away_applied']))
        self.assertEqual([{'key': '127.0.0.1', 'name': '127.0.0.1', 'score': 2}], body['away_devices'])
        self.assertEqual([{'key': CAM, 'name': 'Porch cam'}], body['away_exempt'])
        self.assertEqual('0.0.0.0', await self.ask(TELEMETRY))
        self.assertEqual(REAL_IP, await self.ask('news.example'))
        self.assertEqual('flag', decision.evaluate(TELEMETRY, CAM).action)
        self.assertEqual([{'type': 'away_mode', **body}], self.away_events())
        state = (await self.http.get('/mode')).json()
        self.assertEqual({k: body[k] for k in body}, {k: state[k] for k in body})
        self.assertFalse(state['home_mode'])

        r = await self.http.post('/mode', json={'away': False})
        self.assertEqual({'away': False, 'away_until': None, 'away_applied': True, 'away_devices': [], 'away_exempt': []},
                         r.json())
        self.assertEqual(REAL_IP, await self.ask(TELEMETRY))

    async def test_without_dns_away_is_stored_but_not_acknowledged(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.1):
            r = await self.http.post('/mode', json={'away': True})
        self.assertEqual((True, None, False), (r.json()['away'], r.json()['away_until'], r.json()['away_applied']))
        self.assertFalse(self.away_events()[0]['away_applied'])
        state = (await self.http.get('/mode')).json()
        self.assertEqual((True, False), (state['away'], state['away_applied']))

    async def test_away_reads_off_once_its_end_time_passes(self):
        conn = store.connect(self.path)
        away.write(conn, True, int(time.time() * 1000) - 1, [{'key': '127.0.0.1', 'score': 1}])
        conn.close()
        state = (await self.http.get('/mode')).json()
        self.assertEqual((False, None, []), (state['away'], state['away_until'], state['away_devices']))

    async def test_home_mode_requests_leave_away_alone(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.1):
            r = await self.http.post('/mode', json={'home_mode': True})
        self.assertEqual({'home_mode': True, 'home_manual': True, 'home_applied': False}, r.json())
        self.assertEqual([], self.away_events())
        self.assertTrue(api.state['home_mode'])

    async def test_bad_requests_change_nothing(self):
        now = int(time.time() * 1000)
        for body in ({}, {'away': False, 'away_until': now + HOUR}, {'away': True, 'away_until': now - 1},
                     {'away': True, 'away_until': now + 31 * 24 * HOUR}, {'away': 'soon'}):
            self.assertEqual(422, (await self.http.post('/mode', json=body)).status_code, body)
        self.assertEqual([], self.away_events())

    async def test_away_needs_the_database_and_then_home_mode_is_untouched(self):
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            r = await self.http.post('/mode', json={'away': True, 'home_mode': True})
            self.assertEqual(503, r.status_code)
            self.assertFalse(api.state['home_mode'])
            self.assertFalse((await self.http.get('/mode')).json()['away'])


class WatcherAckTests(unittest.TestCase):
    def test_watcher_records_the_away_revision_it_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'eddie.db')
            conn = store.connect(path)
            engine = decision.Engine()
            watcher = dns_server.StoreWatcher(path, engine)
            watcher.poll()
            rev = away.write(conn, True, None, [{'key': TV, 'score': 1}])
            self.assertEqual(rev - 1, away.loaded(conn))
            self.assertTrue(watcher.poll())
            self.assertEqual(rev, away.loaded(conn))
            self.assertEqual('block', engine.evaluate(TELEMETRY, TV).action)
            self.assertFalse(watcher.poll())
            watcher.conn.close()
            conn.close()


if __name__ == '__main__':
    unittest.main()
