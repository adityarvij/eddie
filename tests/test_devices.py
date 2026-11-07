import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import DNSRecord

from eddie import api, decision, devices, dns_server, feed, rules, store
from tests.test_dns_server import BLOCKED, REAL_IP, Client, FakeUpstream

FIRE_STICK = 'f0:4f:7c:11:22:33'  # Amazon Technologies OUI
PHONE = '7a:11:22:33:44:55'  # locally administered: a phone's private Wi-Fi address

IP_NEIGH = """\
172.20.10.5 dev wlan0 lladdr F0:4F:7C:11:22:33 REACHABLE
172.20.10.3 dev wlan0 lladdr 7a:11:22:33:44:55 STALE
172.20.10.9 dev wlan0  FAILED
172.20.10.1 dev wlan0 lladdr 00:00:00:00:00:00 PERMANENT
fe80::1 dev wlan0 lladdr b8:27:eb:01:02:03 router REACHABLE
"""

ARP_MAC = """\
? (172.20.10.1) at 2:1a:2b:3c:4d:5e on en0 ifscope [ethernet]
? (172.20.10.5) at f0:4f:7c:11:22:33 on en0 ifscope [ethernet]
? (172.20.10.7) at (incomplete) on en0 ifscope [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en0 ifscope permanent [ethernet]
"""


class ParsingTests(unittest.TestCase):
    def test_ip_neigh_keeps_resolved_entries_only(self):
        self.assertEqual({'172.20.10.5': FIRE_STICK, '172.20.10.3': PHONE, 'fe80::1': 'b8:27:eb:01:02:03'},
                         devices.parse_ip_neigh(IP_NEIGH))

    def test_macos_arp_pads_dropped_zeros_and_skips_incomplete_and_multicast(self):
        self.assertEqual({'172.20.10.1': '02:1a:2b:3c:4d:5e', '172.20.10.5': FIRE_STICK},
                         devices.parse_arp(ARP_MAC))

    def test_vendor_from_oui_but_not_from_a_private_address(self):
        self.assertEqual('Amazon', devices.vendor(FIRE_STICK))
        self.assertEqual('Raspberry Pi', devices.vendor('b8:27:eb:01:02:03'))
        self.assertIsNone(devices.vendor(PHONE))
        self.assertIsNone(devices.vendor('00:00:01:00:00:00'))  # Xerox: not a household brand

    def test_dnsmasq_leases(self):
        text = ("1762531200 f0:4f:7c:11:22:33 172.20.10.5 amazon-0a1b2c 01:f0:4f:7c:11:22:33\n"
                "1762531200 7a:11:22:33:44:55 172.20.10.3 * *\n"
                "1762531200 b8:27:eb:01:02:03 172.20.10.4 bad<name> *\n")
        self.assertEqual({FIRE_STICK: 'amazon-0a1b2c'}, devices.parse_leases(text))

    def test_name_prefers_label_then_hostname_then_vendor_then_key(self):
        self.assertEqual('Fire Stick', devices.display_name(FIRE_STICK, 'Fire Stick', 'amazon-0a1b2c', 'Amazon'))
        self.assertEqual('amazon-0a1b2c', devices.display_name(FIRE_STICK, None, 'amazon-0a1b2c', 'Amazon'))
        self.assertEqual('Amazon device', devices.display_name(FIRE_STICK, None, None, 'Amazon'))
        self.assertEqual('172.20.10.9', devices.display_name('172.20.10.9', None, None, None))

    def test_labels(self):
        self.assertEqual('Fire Stick', devices.clean_label('  Fire Stick '))
        self.assertIsNone(devices.clean_label(''))
        self.assertIsNone(devices.clean_label(None))
        for bad in ('x' * 41, 'two\nlines'):
            with self.assertRaises(ValueError):
                devices.clean_label(bad)

    def test_unreadable_arp_table_is_empty_not_an_error(self):
        with patch('subprocess.run', side_effect=FileNotFoundError):
            self.assertEqual({}, devices.read_neighbours())


class DeviceMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.arp = {'172.20.10.5': FIRE_STICK, '172.20.10.3': PHONE}
        self.map = devices.DeviceMap(self.path, read=lambda: self.arp)
        self.conn = store.connect(self.path)

    def tearDown(self):
        self.map.close()
        self.conn.close()
        self.tmp.cleanup()

    def refresh(self, now_ms=1_000):
        self.map.refresh(*self.map.take(), now_ms=now_ms)

    def stored(self):
        return {r[0]: r[1:] for r in self.conn.execute("SELECT mac, ip, vendor, label, first_seen, last_seen FROM devices")}

    def test_key_is_the_ip_until_arp_is_read_then_the_mac(self):
        self.assertEqual('172.20.10.5', self.map.key('172.20.10.5'))
        self.assertTrue(self.map.due(time.monotonic()))
        self.refresh()
        self.assertEqual(FIRE_STICK, self.map.key('172.20.10.5'))

    def test_only_devices_that_asked_eddie_are_recorded(self):
        self.map.key('172.20.10.5')
        self.refresh()
        self.assertEqual({FIRE_STICK: ('172.20.10.5', 'Amazon', None, 1_000, 1_000)}, self.stored())  # not the phone

    def test_refresh_keeps_first_seen_and_label_and_follows_a_new_ip(self):
        self.map.key('172.20.10.5')
        self.refresh(1_000)
        devices.set_label(self.conn, FIRE_STICK, 'Fire Stick')
        self.arp = {'172.20.10.8': FIRE_STICK}
        self.map.key('172.20.10.8')
        self.refresh(2_000)
        self.assertEqual({FIRE_STICK: ('172.20.10.8', 'Amazon', 'Fire Stick', 1_000, 2_000)}, self.stored())

    def test_an_ip_arp_cannot_resolve_triggers_one_read_not_one_per_lookup(self):
        reads = []
        self.map.read = lambda: reads.append(1) or {}
        self.map.key('127.0.0.1')
        self.refresh()
        self.map.key('127.0.0.1')
        self.assertFalse(self.map.due(time.monotonic()))
        self.assertEqual(1, len(reads))

    def test_a_mapped_device_is_recorded_once_then_waits_for_the_periodic_read(self):
        self.refresh()  # ARP read before the device's first lookup
        self.map.key('172.20.10.5')
        self.assertTrue(self.map.due(time.monotonic()))  # mapped but not recorded yet
        self.refresh()
        self.assertIn(FIRE_STICK, self.stored())
        self.map.key('172.20.10.5')
        self.assertFalse(self.map.due(time.monotonic()))

    def test_names_for_the_feed_follow_labels_at_the_next_read(self):
        self.map.key('172.20.10.5')
        self.refresh()
        self.assertEqual('Amazon device', self.map.name(FIRE_STICK))
        self.assertIsNone(self.map.name(PHONE))  # private address: nothing better than its key
        devices.set_label(self.conn, FIRE_STICK, 'Fire Stick')
        self.assertEqual('Amazon device', self.map.name(FIRE_STICK))
        self.refresh()
        self.assertEqual('Fire Stick', self.map.name(FIRE_STICK))

    def test_no_database_still_maps(self):
        m = devices.DeviceMap(None, read=lambda: self.arp)
        m.key('172.20.10.5')
        m.refresh(*m.take())
        self.assertEqual(FIRE_STICK, m.key('172.20.10.5'))

    def test_prune_drops_old_unlabeled_devices_and_keeps_labels(self):
        day = 86_400_000
        self.map.key('172.20.10.5')
        self.map.key('172.20.10.3')
        self.refresh(now_ms=0)
        devices.set_label(self.conn, FIRE_STICK, 'Fire Stick')
        self.assertEqual(1, store.prune(self.conn, 7, now_ms=8 * day))
        self.assertEqual([FIRE_STICK], list(self.stored()))


class DNSDeviceTests(unittest.IsolatedAsyncioTestCase):
    """The DNS server keys lookups and rules by MAC once ARP knows the client."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        p = patch.object(decision, 'engine', decision.Engine())
        p.start()
        self.closers.append(p.stop)
        loop = asyncio.get_running_loop()
        up, _ = await loop.create_datagram_endpoint(FakeUpstream, local_addr=('127.0.0.1', 0))
        self.qlog = store.QueryLog(self.path)
        self.map = devices.DeviceMap(self.path, read=lambda: {'127.0.0.1': FIRE_STICK})
        server = await dns_server.serve('127.0.0.1', 0, [up.get_extra_info('sockname')], 0.3, self.qlog, device_map=self.map)
        client, self.replies = await loop.create_datagram_endpoint(Client, remote_addr=server.get_extra_info('sockname'))
        self.task = asyncio.create_task(dns_server.watch_devices(self.map, every=0.01))
        self.closers[:0] = [self.task.cancel, client.close, server.close, up.close, self.map.close, self.qlog.close]
        self.client = client

    async def asyncTearDown(self):
        for close in self.closers:
            close()

    async def ask(self, name):
        self.client.sendto(DNSRecord.question(name).pack())
        return str(DNSRecord.parse(await asyncio.wait_for(self.replies.replies.get(), 2)).rr[0].rdata)

    async def test_lookups_are_logged_under_the_mac_and_the_device_is_recorded(self):
        await self.ask('news.example')  # first lookup: the IP is new, which triggers a read
        await asyncio.sleep(0.1)
        await self.ask('news.example')
        self.qlog.close()
        conn = store.connect(self.path)
        keys = [r[0] for r in conn.execute("SELECT device_mac FROM queries ORDER BY id")]
        self.assertEqual(FIRE_STICK, keys[-1])
        self.assertEqual(('127.0.0.1', 'Amazon'), conn.execute(
            "SELECT ip, vendor FROM devices WHERE mac = ?", (FIRE_STICK,)).fetchone())
        conn.close()

    async def test_device_rules_match_the_mac_and_still_match_the_old_ip(self):
        await self.ask('news.example')
        await asyncio.sleep(0.1)
        decision.engine.set_rules([decision.Rule(1, 'news.example', 'block', '127.0.0.1')])
        self.assertEqual('0.0.0.0', await self.ask('news.example'))
        decision.engine.set_rules([decision.Rule(2, 'news.example', 'block', FIRE_STICK)])
        self.assertEqual('0.0.0.0', await self.ask('news.example'))
        decision.engine.set_rules([decision.Rule(3, 'news.example', 'block', 'aa:bb:cc:dd:ee:ff')])
        self.assertEqual(REAL_IP, await self.ask('news.example'))


class FeedEventTests(unittest.IsolatedAsyncioTestCase):
    class Feed:
        def __init__(self):
            self.events = []

        def publish(self, event):
            self.events.append(event)

    async def resolve(self, device_map):
        sink = self.Feed()
        await dns_server.resolve(DNSRecord.question(BLOCKED).pack(), '172.20.10.5', [], 0.1, None, sink, device_map)
        return sink.events[0]

    async def test_feed_event_carries_the_mac_and_name(self):
        m = devices.DeviceMap(None, read=lambda: {'172.20.10.5': FIRE_STICK})
        m.refresh(set(), set())
        event = await self.resolve(m)
        self.assertEqual((FIRE_STICK, 'Amazon device'), (event['device_mac'], event['device']))

    async def test_unknown_client_stays_ip_keyed(self):
        event = await self.resolve(devices.DeviceMap(None, read=dict))
        self.assertEqual(('ip:172.20.10.5', '172.20.10.5'), (event['device_mac'], event['device']))
        self.assertEqual(('ip:172.20.10.5', '172.20.10.5'), (lambda e: (e['device_mac'], e['device']))(
            feed.query_event(0, '172.20.10.5', 'a.com', 'A', 'allow', 'default', '')))


class DevicesAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}), patch.dict(api.db, {'conn': None})):
            p.start()
            self.closers.append(p.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie')
        now = int(time.time() * 1000)
        conn = store.connect(self.path)
        devices.upsert(conn, {'172.20.10.5'}, {'172.20.10.5': FIRE_STICK}, {}, now)
        rows = [(now, FIRE_STICK, 'device-metrics-us.amazon.com', 'A', 'block', 'blocklist', ''),
                (now, FIRE_STICK, 'unagi-na.amazon.com', 'A', 'block', 'blocklist', ''),
                (now, FIRE_STICK, 'api.netflix.com', 'A', 'allow', 'allowlist', ''),
                (now, '172.20.10.9', 'news.example', 'A', 'flag', 'default', ''),
                (now - 30 * 3_600_000, FIRE_STICK, 'old.example', 'A', 'block', 'blocklist', '')]
        with conn:
            conn.executemany(f"INSERT INTO queries ({', '.join(store.QUERY_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        conn.close()

    async def asyncTearDown(self):
        await self.http.aclose()
        if api.db['conn']:
            api.db['conn'].close()
        for close in reversed(self.closers):
            close()

    async def listed(self, **params):
        r = await self.http.get('/devices', params=params)
        self.assertEqual(200, r.status_code)
        return {d['mac']: d for d in r.json()['devices']}

    async def test_unlabeled_device_shows_vendor_and_blocked_counts(self):
        found = await self.listed()
        stick = found[FIRE_STICK]
        self.assertEqual(('Amazon device', 'Amazon', '172.20.10.5'), (stick['name'], stick['vendor'], stick['ip']))
        self.assertEqual((3, 2, 0), (stick['queries'], stick['blocked'], stick['flagged']))
        self.assertEqual('172.20.10.9', found['172.20.10.9']['name'])  # known only by IP
        self.assertEqual((1, 0, 1), (found['172.20.10.9']['queries'], found['172.20.10.9']['blocked'],
                                     found['172.20.10.9']['flagged']))
        self.assertEqual(3, (await self.listed(hours=48))[FIRE_STICK]['blocked'])

    async def test_trackers_count_distinct_blocked_domains_and_list_the_latest(self):
        now = int(time.time() * 1000)
        conn = store.connect(self.path)
        with conn:
            conn.executemany(f"INSERT INTO queries ({', '.join(store.QUERY_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [(now - 5000, FIRE_STICK, 'unagi-na.amazon.com', 'A', 'block', 'blocklist', ''),
                              (now + 1000, FIRE_STICK, 'ads.example', 'A', 'block', 'rule', ''),
                              (now + 2000, FIRE_STICK, 'device-metrics-us.amazon.com', 'AAAA', 'block', 'blocklist', '')])
        conn.close()
        stick = (await self.listed())[FIRE_STICK]
        self.assertEqual((5, 3), (stick['blocked'], stick['trackers']))  # repeats count once; old.example is outside 24 h
        self.assertEqual(['device-metrics-us.amazon.com', 'ads.example', 'unagi-na.amazon.com'],
                         [b['domain'] for b in stick['recent_blocks']])
        self.assertEqual(now + 2000, stick['recent_blocks'][0]['ts'])
        self.assertEqual(4, (await self.listed(hours=48))[FIRE_STICK]['trackers'])
        laptop = (await self.listed())['172.20.10.9']
        self.assertEqual((0, []), (laptop['trackers'], laptop['recent_blocks']))  # flagged isn't blocked

    async def test_labeled_device_shows_its_label(self):
        r = await self.http.put(f'/devices/{FIRE_STICK.upper()}', json={'label': 'Fire Stick'})
        self.assertEqual(200, r.status_code)
        self.assertEqual('Fire Stick', r.json()['name'])
        self.assertEqual('Fire Stick', (await self.listed())[FIRE_STICK]['name'])
        await self.http.put(f'/devices/{FIRE_STICK}', json={'label': ''})
        self.assertEqual('Amazon device', (await self.listed())[FIRE_STICK]['name'])

    async def test_a_device_known_only_by_ip_can_be_labeled(self):
        r = await self.http.put('/devices/172.20.10.9', json={'label': 'Laptop'})
        self.assertEqual(200, r.status_code)
        self.assertEqual(('172.20.10.9', 'Laptop'), (r.json()['ip'], (await self.listed())['172.20.10.9']['name']))

    async def test_bad_requests(self):
        self.assertEqual(404, (await self.http.put('/devices/aa:bb:cc:dd:ee:ff', json={'label': 'TV'})).status_code)
        self.assertEqual(422, (await self.http.put(f'/devices/{FIRE_STICK}', json={'label': 'x' * 41})).status_code)
        self.assertEqual(422, (await self.http.put('/devices/bad%20key', json={'label': 'TV'})).status_code)
        self.assertEqual(422, (await self.http.get('/devices', params={'hours': 0})).status_code)

    async def test_off_switch(self):
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            self.assertEqual(503, (await self.http.get('/devices')).status_code)

    async def test_device_rule_by_mac_is_accepted(self):
        stored, _ = rules.add(api.rules_db(), rules.RuleIn(pattern='a.com', action='block', device_mac=FIRE_STICK))
        self.assertEqual(FIRE_STICK, stored['device_mac'])


if __name__ == '__main__':
    unittest.main()
