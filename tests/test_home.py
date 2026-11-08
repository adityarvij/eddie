"""Home mode (#20): household phone presence, the camera block, manual overrides and DNS acknowledgment."""
import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import DNSRecord

from eddie import api, decision, dns_server, home, store
from tests.test_dns_server import REAL_IP, Client, FakeUpstream
from tests.test_rules_api import FakeSocket

PHONE, OTHER_PHONE, LAPTOP = 'aa:00:00:00:00:01', 'aa:00:00:00:00:02', 'aa:00:00:00:00:03'
CAMERA = 'fw-upload.ring.com'
MINUTE = 60_000


def seen(conn, mac, ago_ms, phone=True):
    now = int(time.time() * 1000)
    conn.execute("INSERT INTO devices (mac, last_seen, phone) VALUES (?, ?, ?)"
                 " ON CONFLICT(mac) DO UPDATE SET last_seen = excluded.last_seen", (mac, now - ago_ms, int(phone)))
    conn.commit()


class EngineTests(unittest.TestCase):
    def test_home_mode_blocks_camera_uploads_only(self):
        engine = decision.Engine()
        self.assertEqual('flag', engine.evaluate(CAMERA, LAPTOP).action)
        engine.set_home_mode(True)
        self.assertEqual(decision.Decision('block', 'home_mode', 'home mode: ring.com camera upload blocked'),
                         engine.evaluate(CAMERA, LAPTOP))
        self.assertEqual('block', engine.evaluate('ring.com', LAPTOP).action)
        self.assertNotEqual('home_mode', engine.evaluate('netflix.com', LAPTOP).source)
        self.assertNotEqual('home_mode', engine.evaluate('notring.com', LAPTOP).source)

    def test_rules_and_the_allowlist_still_come_first(self):
        engine = decision.Engine()
        engine.set_home_mode(True)
        engine.set_rules([decision.Rule(1, '*.ring.com', 'allow', LAPTOP)])
        self.assertEqual('allow', engine.evaluate(CAMERA, LAPTOP).action)
        engine.set_rules([])
        engine.set_lists(['ring.com'], [])
        self.assertEqual('allowlist', engine.evaluate(CAMERA, LAPTOP).source)


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(os.path.join(self.tmp.name, 'eddie.db'))
        self.addCleanup(self.conn.close)

    def test_only_marked_phones_seen_in_the_last_ten_minutes_count(self):
        now = int(time.time() * 1000)
        seen(self.conn, PHONE, 9 * MINUTE)
        seen(self.conn, OTHER_PHONE, 11 * MINUTE)
        seen(self.conn, LAPTOP, MINUTE, phone=False)
        self.assertEqual([PHONE], home.phones_home(self.conn, now))
        self.assertEqual([], home.phones_home(self.conn, now + 2 * MINUTE))

    def test_a_recent_lookup_counts_as_seen(self):
        now = int(time.time() * 1000)
        seen(self.conn, PHONE, 60 * MINUTE)
        self.conn.execute("INSERT INTO queries (ts, device_mac, domain) VALUES (?, ?, 'apple.com')", (now - MINUTE, PHONE))
        self.assertEqual([PHONE], home.phones_home(self.conn, now))

    def test_marked_phones_survive_pruning_and_old_databases_gain_the_column(self):
        seen(self.conn, PHONE, 30 * 86_400_000)
        seen(self.conn, LAPTOP, 30 * 86_400_000, phone=False)
        store.prune(self.conn, 7)
        self.assertEqual([(PHONE,)], self.conn.execute("SELECT mac FROM devices").fetchall())
        path = os.path.join(self.tmp.name, 'old.db')
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE devices (mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, vendor TEXT, label TEXT,"
                    " first_seen INTEGER, last_seen INTEGER, kid INTEGER NOT NULL DEFAULT 0)")
        old.close()
        upgraded = store.connect(path)
        self.assertIn('phone', {r[1] for r in upgraded.execute("PRAGMA table_info(devices)")})
        upgraded.close()

    def test_watcher_records_the_home_revision_it_loaded(self):
        engine = decision.Engine()
        watcher = dns_server.StoreWatcher(os.path.join(self.tmp.name, 'eddie.db'), engine)
        self.addCleanup(watcher.close)
        watcher.poll()
        rev = home.write(self.conn, True, False)
        self.assertEqual(rev - 1, home.loaded(self.conn))
        self.assertTrue(watcher.poll())
        self.assertEqual(rev, home.loaded(self.conn))
        self.assertEqual('block', engine.evaluate(CAMERA, LAPTOP).action)
        self.assertFalse(watcher.poll())


class HomeAPITests(unittest.IsolatedAsyncioTestCase):
    """Presence, overrides and /mode against a real DNS server, both reading one eddie.db."""

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
        self.conn = store.connect(self.path)
        self.closers.append(self.conn.close)
        seen(self.conn, PHONE, 60 * MINUTE, phone=False)  # a device Eddie knows, not yet marked

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
        self.closers += [up.close, server.close, client.close, task.cancel, watcher.close]
        self.dns = client, proto
        await asyncio.sleep(0.05)

    async def ask(self, name):
        client, proto = self.dns
        client.sendto(DNSRecord.question(name).pack())
        return str(DNSRecord.parse(await asyncio.wait_for(proto.replies.get(), 2)).rr[0].rdata)

    def mode_events(self):
        return [m for m in self.ws.sent if m['type'] == 'mode']

    async def mark_phone(self, phone=True):
        r = await self.http.put(f'/devices/{PHONE}', json={'phone': phone})
        self.assertEqual(200, r.status_code)
        self.assertIs(phone, r.json()['phone'])

    async def test_phone_joins_then_leaves_and_dns_follows(self):
        await self.start_dns()
        await self.mark_phone()
        self.assertTrue((await self.http.get('/devices')).json()['devices'][0]['phone'])
        self.assertIsNone(await api.check_presence())  # marked, but last seen an hour ago
        seen(self.conn, PHONE, 0)
        self.assertEqual({'home_mode': True, 'home_manual': False, 'home_applied': True}, await api.check_presence())
        self.assertEqual('0.0.0.0', await self.ask(CAMERA))
        self.assertEqual(REAL_IP, await self.ask('netflix.com'))
        self.assertEqual([{'type': 'mode', 'home_mode': True, 'home_manual': False, 'home_applied': True,
                           'reason': 'presence'}], self.mode_events())
        self.assertIsNone(await api.check_presence())  # still home: no second event

        later = int(time.time() * 1000) + home.PRESENT_MS + MINUTE
        self.assertEqual({'home_mode': False, 'home_manual': False, 'home_applied': True},
                         await api.check_presence(later))
        self.assertEqual(REAL_IP, await self.ask(CAMERA))
        self.assertEqual('presence', self.mode_events()[-1]['reason'])

    async def test_unmarked_phones_never_turn_it_on(self):
        seen(self.conn, PHONE, 0)
        self.assertIsNone(await api.check_presence())
        await self.mark_phone()
        await self.mark_phone(False)
        self.assertIsNone(await api.check_presence())
        self.assertEqual([], self.mode_events())

    async def test_a_manual_off_holds_while_phones_come_and_go_until_handed_back(self):
        await self.start_dns()
        await self.mark_phone()
        seen(self.conn, PHONE, 0)
        await api.check_presence()
        r = await self.http.post('/mode', json={'home_mode': False})
        self.assertEqual({'home_mode': False, 'home_manual': True, 'home_applied': True}, r.json())
        self.assertEqual('manual', self.mode_events()[-1]['reason'])
        self.assertEqual(REAL_IP, await self.ask(CAMERA))

        later = int(time.time() * 1000) + home.PRESENT_MS + MINUTE
        self.assertIsNone(await api.check_presence(later))  # everyone leaves
        self.assertIsNone(await api.check_presence())  # and the phone joins again
        state = (await self.http.get('/mode')).json()
        self.assertEqual((False, True, True), (state['home_mode'], state['home_manual'], state['home_applied']))

        r = await self.http.post('/mode', json={'home_auto': True})
        self.assertEqual({'home_mode': True, 'home_manual': False, 'home_applied': True}, r.json())
        self.assertEqual('presence', self.mode_events()[-1]['reason'])
        self.assertEqual('0.0.0.0', await self.ask(CAMERA))

    async def test_a_manual_on_holds_after_the_last_phone_leaves(self):
        await self.mark_phone()
        with patch.object(api, 'APPLY_TIMEOUT', 0.1):
            await self.http.post('/mode', json={'home_mode': True})
        self.assertIsNone(await api.check_presence())
        self.assertTrue(home.wanted(self.conn))

    async def test_without_dns_the_event_says_it_was_not_applied(self):
        await self.mark_phone()
        seen(self.conn, PHONE, 0)
        with patch.object(api, 'APPLY_TIMEOUT', 0.1):
            result = await api.check_presence()
        self.assertEqual({'home_mode': True, 'home_manual': False, 'home_applied': False}, result)
        self.assertFalse(self.mode_events()[0]['home_applied'])
        self.assertFalse((await self.http.get('/mode')).json()['home_applied'])

    async def test_bad_requests_change_nothing(self):
        for body in ({'home_auto': False}, {'home_auto': True, 'home_mode': True}):
            self.assertEqual(422, (await self.http.post('/mode', json=body)).status_code, body)
        self.assertEqual(422, (await self.http.put(f'/devices/{PHONE}', json={'phone': 1})).status_code)
        self.assertEqual(404, (await self.http.put('/devices/aa:00:00:00:00:09', json={'phone': True})).status_code)
        self.assertEqual([], self.mode_events())
        self.assertEqual(0, home.revision(self.conn))


if __name__ == '__main__':
    unittest.main()
