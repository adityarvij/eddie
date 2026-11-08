import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx2 as httpx

from eddie import api, rollback, rules, store

T0 = 1_762_531_200_000  # when the rule is added
MIN = 60_000
TV, PHONE = 'f0:4f:7c:11:22:33', 'a4:83:e7:00:00:01'
DOMAIN = 'device-metrics-us.amazon.com'


def rule(rule_id=1, pattern=DOMAIN, device_mac=TV, action='block', pinned=False, ts=T0):
    return {'id': rule_id, 'scope': 'device' if device_mac else 'global', 'device_mac': device_mac,
            'pattern': pattern, 'action': action, 'created_by': 'user', 'ts': ts, 'pinned': pinned}


def lookup(ts, domain='netflix.com', device_mac=TV, device='Fire Stick', action='allow', source='default', reason=''):
    return {'type': 'query', 'ts': ts, 'device_mac': device_mac, 'device': device, 'domain': domain,
            'qtype': 'A', 'action': action, 'source': source, 'reason': reason}


def blocked(ts, domain=DOMAIN, device_mac=TV, device='Fire Stick', scope='device', pattern=DOMAIN):
    return lookup(ts, domain, device_mac, device, 'block', 'rule', f'{scope} rule {pattern}')


def normal_traffic(watcher, start, end, every=10_000, device_mac=TV):
    """A device's ordinary lookups: one allowed lookup every `every` ms (6 a minute by default)."""
    out = []
    for ts in range(start, end, every):
        out += watcher.observe(lookup(ts, device_mac=device_mac))
    return out


class RetryStormTests(unittest.TestCase):
    def setUp(self):
        self.w = rollback.Watcher()
        normal_traffic(self.w, T0 - 10 * MIN, T0)
        self.w.watch(rule())

    def test_twelve_blocked_lookups_in_a_minute_roll_back(self):
        found = []
        for i in range(12):  # every 4 s: the feed sends a repeat at most every 2 s
            found += self.w.observe(blocked(T0 + 30_000 + i * 4000))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].sign, 'retry_storm')
        self.assertEqual(found[0].rule['id'], 1)
        self.assertEqual(found[0].device_mac, TV)
        self.assertIn('12 times in a minute', found[0].reason)
        self.assertIn("guess, not proof", found[0].reason)
        self.assertNotIn(1, self.w.watches, 'a rolled-back rule is no longer watched')

    def test_healthy_retries_once_a_minute_never_roll_back(self):
        found = normal_traffic(self.w, T0, T0 + 10 * MIN)
        for i in range(10):  # a device honouring the 60 s block TTL
            found += self.w.observe(blocked(T0 + i * MIN + 5000))
        self.assertEqual(found, [])

    def test_eleven_in_a_minute_is_under_the_threshold(self):
        found = []
        for i in range(11):
            found += self.w.observe(blocked(T0 + i * 5000))
        self.assertEqual(found, [])

    def test_storm_after_the_watch_window_is_ignored(self):
        found = []
        for i in range(20):
            found += self.w.observe(blocked(T0 + rollback.WATCH_MS + MIN + i * 2000))
        self.assertEqual(found, [])
        self.assertEqual(self.w.watches, {})

    def test_other_devices_blocked_by_their_own_rule_dont_count(self):
        found = []
        for i in range(20):
            found += self.w.observe(blocked(T0 + i * 2000, device_mac=PHONE, device='Phone'))
        self.assertEqual(found, [])

    def test_lookups_blocked_by_a_list_dont_count(self):
        found = []
        for i in range(20):
            found += self.w.observe(lookup(T0 + i * 2000, DOMAIN, action='block', source='blocklist', reason=DOMAIN))
        self.assertEqual(found, [])

    def test_global_rule_rolls_back_on_one_device_storming(self):
        w = rollback.Watcher()
        w.watch(rule(2, '*.amazon.com', None))
        found = []
        for i in range(12):
            found += w.observe(blocked(T0 + i * 3000, 'unagi-na.amazon.com', PHONE, 'Phone', 'global', '*.amazon.com'))
        self.assertEqual([(f.rule['id'], f.device) for f in found], [(2, 'Phone')])
        self.assertIn('the block on *.amazon.com for Phone', found[0].reason)

    def test_device_rule_by_ip_matches_an_ip_keyed_device(self):
        w = rollback.Watcher()
        w.watch(rule(3, device_mac='172.20.10.5'))
        found = []
        for i in range(12):
            found += w.observe(blocked(T0 + i * 3000, device_mac='ip:172.20.10.5', device='172.20.10.5'))
        self.assertEqual(len(found), 1)


class TrafficCollapseTests(unittest.TestCase):
    def setUp(self):
        self.w = rollback.Watcher()
        normal_traffic(self.w, T0 - 10 * MIN, T0)  # 60 other lookups in the 10 minutes before
        self.w.watch(rule())

    def test_other_traffic_stopping_while_retrying_rolls_back(self):
        found = []
        for i in range(4):  # still asking once a minute, nothing else
            found += self.w.observe(blocked(T0 + 10_000 + i * MIN))
        self.assertEqual([f.sign for f in found], ['traffic_collapse'])
        self.assertIn('fell from about 6 to 0 a minute', found[0].reason)
        self.assertIn('guess, not proof', found[0].reason)

    def test_not_before_three_minutes(self):
        found = []
        for i in range(3):
            found += self.w.observe(blocked(T0 + 10_000 + i * 50_000))  # last one 100 s after the first
        self.assertEqual(found, [])

    def test_a_device_that_went_quiet_entirely_is_not_rolled_back(self):
        found = self.w.observe(blocked(T0 + 10_000))  # one lookup, then switched off
        found += self.w.observe(lookup(T0 + 8 * MIN, device_mac=PHONE, device='Phone'))
        self.assertEqual(found, [])

    def test_traffic_that_keeps_flowing_is_not_a_collapse(self):
        found = []
        for i in range(6):
            found += normal_traffic(self.w, T0 + i * MIN, T0 + (i + 1) * MIN, every=20_000)  # halved, not collapsed
            found += self.w.observe(blocked(T0 + i * MIN + 5000))
        self.assertEqual(found, [])

    def test_a_quiet_device_has_no_baseline(self):
        w = rollback.Watcher()
        normal_traffic(w, T0 - 10 * MIN, T0, every=60_000)  # 10 lookups: too few to judge a drop
        w.watch(rule())
        found = []
        for i in range(6):
            found += w.observe(blocked(T0 + 10_000 + i * MIN))
        self.assertEqual(found, [])


class WatchTests(unittest.TestCase):
    def test_only_unpinned_block_rules_are_watched(self):
        w = rollback.Watcher()
        for r in (rule(1, pinned=True), rule(2, action='allow'), rule(3, action='spoof'), rule(4, '*')):
            w.watch(r)
        self.assertEqual(w.watches, {})
        w.watch(rule(5))
        self.assertEqual(list(w.watches), [5])

    def test_history_keeps_times_only_and_is_bounded_in_time(self):
        w = rollback.Watcher()
        normal_traffic(w, T0, T0 + 60 * MIN)
        times = w.history[TV]
        self.assertTrue(all(isinstance(t, int) for t in times))
        self.assertGreaterEqual(times[0], times[-1] - rollback.HISTORY_MS)
        self.assertLess(times[0], times[-1] - rollback.HISTORY_MS + 10_000)


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    def of(self, kind):
        return [m for m in self.sent if m['type'] == kind]


class RollbackAPITests(unittest.IsolatedAsyncioTestCase):
    """Synthetic traffic through /ingest against a real eddie.db; no DNS server, so applied is false."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}),
                  patch.dict(api.db, {'conn': None}),
                  patch.object(api, 'clients', set()),
                  patch.object(api, 'watcher', rollback.Watcher()),
                  patch.object(api, 'recent', api.deque(maxlen=20000)),
                  patch.object(api, 'FEED_RETENTION_MS', 10 ** 13),
                  patch.object(api, 'APPLY_TIMEOUT', 0.05)):
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

    async def add(self, **extra):
        r = await self.http.post('/rules', json={'pattern': DOMAIN, 'action': 'block', 'device_mac': TV, **extra})
        self.assertEqual(r.status_code, 201)
        return r.json()

    async def storm(self, start):
        events = [blocked(start + i * 3000) for i in range(12)]
        r = await self.http.post('/ingest', json=events)
        self.assertEqual(r.status_code, 200)
        await asyncio.gather(*list(api.rollbacks))

    async def test_storm_deletes_the_rule_through_rules_and_explains(self):
        row = await self.add()
        self.assertFalse(row['pinned'])
        await self.storm(row['ts'])
        self.assertEqual((await self.http.get('/rules')).json(), [])
        removed = self.ws.of('rule_change')[-1]
        self.assertEqual((removed['change'], removed['id']), ('removed', row['id']))
        [event] = self.ws.of('rule_rolled_back')
        self.assertEqual(event['rule']['id'], row['id'])
        self.assertEqual(event['rule']['pattern'], DOMAIN)
        self.assertEqual((event['device_mac'], event['device'], event['sign']), (TV, 'Fire Stick', 'retry_storm'))
        self.assertIn("That's a guess, not proof it broke.", event['reason'])
        self.assertIs(event['applied'], False, 'no DNS server loaded the delete')
        self.assertLess(self.ws.sent.index(removed), self.ws.sent.index(event))

    async def test_pinned_rule_is_never_rolled_back(self):
        row = await self.add(pinned=True)
        self.assertTrue(row['pinned'])
        await self.storm(row['ts'])
        self.assertEqual([r['id'] for r in (await self.http.get('/rules')).json()], [row['id']])
        self.assertEqual(self.ws.of('rule_rolled_back'), [])

    async def test_pinning_after_adding_stops_the_watch(self):
        row = await self.add()
        r = await self.http.post(f"/rules/{row['id']}/pin")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['pinned'])
        await self.storm(row['ts'])
        self.assertEqual(len((await self.http.get('/rules')).json()), 1)
        self.assertEqual(self.ws.of('rule_rolled_back'), [])
        self.assertEqual((await self.http.post('/rules/999/pin')).status_code, 404)

    async def test_rule_deleted_by_the_user_meanwhile_is_left_alone(self):
        row = await self.add()
        found = api.watcher.watches[row['id']]
        await self.http.delete(f"/rules/{row['id']}")
        again = await self.add()  # SQLite may hand the freed id to the next rule
        await api.roll_back(rollback.Rollback(found.rule, TV, 'Fire Stick', 'retry_storm', 'x'))
        self.assertEqual([r['id'] for r in (await self.http.get('/rules')).json()], [again['id']])
        self.assertEqual(self.ws.of('rule_rolled_back'), [])

    async def test_allow_rule_is_never_rolled_back(self):
        r = await self.http.post('/rules', json={'pattern': DOMAIN, 'action': 'allow', 'device_mac': TV})
        await self.storm(r.json()['ts'])
        self.assertEqual(len((await self.http.get('/rules')).json()), 1)


class PinnedColumnTests(unittest.TestCase):
    def test_an_old_eddie_db_gains_the_pinned_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'eddie.db')
            old = sqlite3.connect(path)
            old.execute('CREATE TABLE rules (id INTEGER PRIMARY KEY, scope TEXT, device_mac TEXT, pattern TEXT, '
                        'action TEXT, created_by TEXT, ts INTEGER)')
            old.execute("INSERT INTO rules VALUES (1, 'global', NULL, 'a.com', 'block', 'user', 5)")
            old.commit()
            old.close()
            conn = store.connect(path)
            self.assertEqual(rules.all_rules(conn)[0]['pinned'], False)
            conn.close()


if __name__ == '__main__':
    unittest.main()
