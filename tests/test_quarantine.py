"""New device alert and quarantine (#98): first sighting, quarantine, approval and denial."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import DNSRecord

from eddie import api, decision, devices, dns_server, quarantine, store
from tests.test_dns_server import REAL_IP, Client, FakeUpstream

FIRE_STICK = 'f0:4f:7c:11:22:33'  # Amazon OUI: the household, there before quarantine
NEWCOMER = 'f0:4f:7c:aa:bb:cc'  # Amazon OUI, joins later
PHONE = '7a:11:22:33:44:55'  # private Wi-Fi address: no vendor
ESSENTIAL = 'time.apple.com'


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(os.path.join(self.tmp.name, 'eddie.db'))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_the_first_devices_are_the_household_and_raise_no_alert(self):
        devices.upsert(self.conn, {'172.20.10.9'}, {'172.20.10.9': PHONE}, {}, 1)  # recorded before #98
        self.assertEqual([], quarantine.record_new(self.conn, [FIRE_STICK], 1_000))
        self.assertEqual({FIRE_STICK: 'approved', PHONE: 'approved'}, quarantine.statuses(self.conn))

    def test_a_mac_seen_later_is_pending_once(self):
        quarantine.record_new(self.conn, [FIRE_STICK], 1_000)
        rev = quarantine.revision(self.conn)
        self.assertEqual([NEWCOMER], quarantine.record_new(self.conn, [FIRE_STICK, NEWCOMER], 2_000))
        self.assertEqual([], quarantine.record_new(self.conn, [NEWCOMER], 3_000))
        self.assertEqual('pending', quarantine.statuses(self.conn)[NEWCOMER])
        self.assertEqual(rev + 1, quarantine.revision(self.conn))  # DNS reloads the new device

    def test_pending_is_held_only_while_quarantine_is_on_and_denied_always(self):
        quarantine.record_new(self.conn, [FIRE_STICK], 1_000)
        quarantine.record_new(self.conn, [NEWCOMER, PHONE], 2_000)
        self.assertFalse(quarantine.wanted(self.conn))
        self.assertEqual(frozenset(), quarantine.blocked(self.conn))
        quarantine.write(self.conn, True)
        self.assertEqual({NEWCOMER, PHONE}, quarantine.blocked(self.conn))
        quarantine.decide(self.conn, PHONE, 'denied')
        quarantine.decide(self.conn, NEWCOMER, 'approved')
        self.assertEqual({PHONE}, quarantine.blocked(self.conn))
        quarantine.write(self.conn, False)
        self.assertEqual({PHONE}, quarantine.blocked(self.conn))

    def test_only_recorded_devices_can_be_decided(self):
        self.assertEqual(None, quarantine.decide(self.conn, NEWCOMER, 'approved')[0])
        with self.assertRaises(ValueError):
            quarantine.decide(self.conn, NEWCOMER, 'pending')

    def test_prune_forgets_an_undecided_device_that_left_and_keeps_decisions(self):
        quarantine.record_new(self.conn, [FIRE_STICK], 0)
        devices.upsert(self.conn, {'a', 'b'}, {'a': NEWCOMER, 'b': PHONE}, {}, 0)
        quarantine.record_new(self.conn, [NEWCOMER, PHONE], 0)
        quarantine.decide(self.conn, PHONE, 'denied', 0)
        store.prune(self.conn, 7, now_ms=30 * 86_400_000)
        self.assertEqual({FIRE_STICK: 'approved', PHONE: 'denied'}, quarantine.statuses(self.conn))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = decision.Engine()
        self.engine.set_lists({'apple.com'}, set())

    def test_a_quarantined_device_keeps_only_the_essentials(self):
        self.engine.set_quarantined({NEWCOMER: decision.QUARANTINE})
        self.engine.set_rules([decision.Rule(1, 'news.example', 'allow', NEWCOMER)])
        self.assertEqual(('block', 'quarantine', decision.QUARANTINE),
                         tuple(vars(self.engine.evaluate('news.example', NEWCOMER)).values()))
        self.assertEqual('allow', self.engine.evaluate(ESSENTIAL, NEWCOMER).action)
        self.assertEqual('flag', self.engine.evaluate('other.example', FIRE_STICK).action)


class DeviceMapAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.arp = {'172.20.10.5': FIRE_STICK}
        self.map = devices.DeviceMap(self.path, read=lambda: self.arp)

    def tearDown(self):
        self.map.close()
        self.tmp.cleanup()

    def lookup(self, ip, now_ms):
        self.map.key(ip)
        return self.map.refresh(*self.map.take(), now_ms=now_ms)

    def test_first_sighting_alerts_with_time_vendor_and_hostname_once(self):
        self.assertEqual([], self.lookup('172.20.10.5', 1_000))
        self.arp = {'172.20.10.5': FIRE_STICK, '172.20.10.8': NEWCOMER}
        with patch.object(devices, 'read_leases', return_value={NEWCOMER: 'kindle'}):
            alerts = self.lookup('172.20.10.8', 8_040_000)
            self.assertEqual([{'type': 'new_device', 'ts': 8_040_000, 'device_mac': NEWCOMER, 'device': 'kindle',
                               'ip': '172.20.10.8', 'vendor': 'Amazon', 'hostname': 'kindle', 'quarantine': False}],
                             alerts)
            self.assertEqual([], self.lookup('172.20.10.8', 8_100_000))

    def test_alert_says_whether_quarantine_was_on(self):
        self.lookup('172.20.10.5', 1_000)
        quarantine.write(self.map.conn, True)
        self.arp = {'172.20.10.3': PHONE}
        [alert] = self.lookup('172.20.10.3', 2_000)
        self.assertEqual((PHONE, None, True), (alert['device'], alert['vendor'], alert['quarantine']))

    def test_no_database_no_alerts(self):
        m = devices.DeviceMap(None, read=lambda: self.arp)
        m.key('172.20.10.5')
        self.assertEqual([], m.refresh(*m.take()))


class QuarantineDNSTests(unittest.IsolatedAsyncioTestCase):
    """The API and a real DNS server on one eddie.db: quarantine is enforced only once DNS acknowledges it."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}), patch.object(decision, 'engine', decision.Engine()),
                  patch.dict(api.db, {'conn': None}), patch.object(api, 'clients', set())):
            p.start()
            self.closers.append(p.stop)
        decision.engine.set_lists({'apple.com'}, set())
        self.ws = FakeSocket()
        api.clients.add(self.ws)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie')
        conn = store.connect(self.path)
        now = int(time.time() * 1000)
        devices.upsert(conn, {'a'}, {'a': FIRE_STICK}, {}, now)
        quarantine.record_new(conn, [FIRE_STICK])  # the household
        devices.upsert(conn, {'b'}, {'b': NEWCOMER}, {}, now)
        quarantine.record_new(conn, [NEWCOMER])  # 127.0.0.1 is the newcomer below
        conn.close()

    async def asyncTearDown(self):
        await self.http.aclose()
        if api.db['conn']:
            api.db['conn'].close()
        for close in self.closers:
            close()

    async def start_dns(self):
        loop = asyncio.get_running_loop()
        up, _ = await loop.create_datagram_endpoint(FakeUpstream, local_addr=('127.0.0.1', 0))
        self.map = devices.DeviceMap(None, read=lambda: {'127.0.0.1': NEWCOMER})
        self.map.key('127.0.0.1')
        self.map.refresh(*self.map.take())
        server = await dns_server.serve('127.0.0.1', 0, [up.get_extra_info('sockname')], 0.3, device_map=self.map)
        client, self.replies = await loop.create_datagram_endpoint(Client, remote_addr=server.get_extra_info('sockname'))
        watcher = dns_server.StoreWatcher(self.path, decision.engine)
        task = asyncio.create_task(dns_server.watch_store(watcher, every=0.01))
        self.closers[:0] = [task.cancel, watcher.conn.close, client.close, server.close, up.close]
        self.client = client

    async def ask(self, name):
        self.client.sendto(DNSRecord.question(name).pack())
        return str(DNSRecord.parse(await asyncio.wait_for(self.replies.replies.get(), 2)).rr[0].rdata)

    async def test_quarantine_then_approval(self):
        await self.start_dns()
        self.assertEqual(REAL_IP, await self.ask('news.example'))  # quarantine is opt-in

        r = await self.http.post('/quarantine', json={'quarantine': True})
        self.assertEqual({'quarantine': True, 'applied': True}, r.json())
        self.assertEqual('0.0.0.0', await self.ask('news.example'))
        self.assertEqual(REAL_IP, await self.ask(ESSENTIAL))
        listed = (await self.http.get('/devices')).json()
        self.assertEqual((True, True), (listed['quarantine'], listed['quarantine_applied']))
        self.assertEqual({FIRE_STICK: 'approved', NEWCOMER: 'pending'},
                         {d['mac']: d['status'] for d in listed['devices']})

        r = await self.http.post(f'/devices/{NEWCOMER}/approve')
        self.assertEqual({'mac': NEWCOMER, 'name': 'Amazon device', 'status': 'approved', 'applied': True}, r.json())
        self.assertEqual(REAL_IP, await self.ask('news.example'))
        self.assertEqual({'type': 'device_status', 'mac': NEWCOMER, 'name': 'Amazon device',
                          'status': 'approved', 'applied': True}, self.ws.sent[-1])

    async def test_denied_stays_blocked_with_quarantine_off(self):
        await self.start_dns()
        r = await self.http.post(f'/devices/{NEWCOMER}/deny')
        self.assertEqual(('denied', True), (r.json()['status'], r.json()['applied']))
        self.assertEqual('0.0.0.0', await self.ask('news.example'))

    async def test_without_dns_a_decision_is_stored_but_not_acknowledged(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.05):
            r = await self.http.post(f'/devices/{NEWCOMER}/approve')
            self.assertEqual(('approved', False), (r.json()['status'], r.json()['applied']))
            r = await self.http.post('/quarantine', json={'quarantine': True})
        self.assertEqual({'quarantine': True, 'applied': False}, r.json())
        self.assertFalse((await self.http.get('/mode')).json()['quarantine_applied'])

    async def test_bad_requests(self):
        self.assertEqual(404, (await self.http.post('/devices/aa:bb:cc:dd:ee:ff/approve')).status_code)
        self.assertEqual(422, (await self.http.post('/devices/172.20.10.9/approve')).status_code)
        self.assertEqual(422, (await self.http.post('/quarantine', json={'quarantine': 'maybe'})).status_code)

    async def test_new_device_alert_is_broadcast(self):
        alert = {'type': 'new_device', 'ts': 1, 'device_mac': NEWCOMER, 'device': 'Amazon device',
                 'ip': '172.20.10.8', 'vendor': 'Amazon', 'hostname': None, 'quarantine': False}
        r = await self.http.post('/devices/new', json=[alert])
        self.assertEqual({'ok': True, 'count': 1}, r.json())
        self.assertEqual(alert, self.ws.sent[-1])
        self.assertEqual(422, (await self.http.post('/devices/new', json={**alert, 'device_mac': 'nope'})).status_code)


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_devices_are_sent_to_the_api(self):
        class Map:
            def __init__(self):
                self.reads = 0

            def due(self, now):
                return self.reads == 0

            def take(self):
                return set(), set()

            def refresh(self, seen, new):
                self.reads += 1
                return [{'device_mac': NEWCOMER}]

        class Feed:
            sent = []

            def send_new_devices(self, alerts):
                self.sent.append(alerts)

        feed = Feed()
        task = asyncio.create_task(dns_server.watch_devices(Map(), every=0.01, feed=feed))
        await asyncio.sleep(0.05)
        task.cancel()
        self.assertEqual([[{'device_mac': NEWCOMER}]], feed.sent)


if __name__ == '__main__':
    unittest.main()
