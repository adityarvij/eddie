"""ACR blocker (#104) on synthetic beacon traffic: seed list, DNS decisions, acr_detected events."""
import json
import random
import time
import unittest
from unittest.mock import patch

from eddie import acr, api, beacons
from eddie.acr import Sightings
from eddie.beacons import Beacons, Idle
from eddie.decision import Engine, Rule

S = 1000
TV = 'f0:4f:7c:11:22:33'
PHONE = 'aa:bb:cc:dd:ee:02'
ACR_DOMAIN = 'acr.samsungacr.com'  # no tracker keyword, so only the ACR step can block it


def timer(every_ms, n=12, jitter=0.03, start=None, seed=1):
    """Lookup times of a device phoning home every every_ms, plus or minus jitter."""
    rng = random.Random(seed)
    ts = [start if start is not None else int(time.time() * 1000) - n * every_ms]
    for _ in range(n - 1):
        ts.append(ts[-1] + round(every_ms * (1 + rng.uniform(-jitter, jitter))))
    return ts


def scattered(n=12, start=None, seed=2):
    rng = random.Random(seed)
    ts = [start if start is not None else int(time.time() * 1000) - 3_600_000]
    for _ in range(n - 1):
        ts.append(ts[-1] + rng.choice([4 * S, 45 * S, 3 * 60 * S, 11 * 60 * S]))
    return ts


class SeedTests(unittest.TestCase):
    def test_every_entry_names_a_provider_and_a_source(self):
        self.assertGreaterEqual(len(acr.ACR), 4)
        for name, (provider, source) in acr.ACR.items():
            self.assertEqual(name, name.strip().lower().rstrip('.'))
            self.assertTrue(provider and source, name)

    def test_entries_cover_subdomains_only(self):
        self.assertEqual(('samsungacr.com', 'Samsung'), acr.endpoint('Log-1.SamsungACR.com.'))
        self.assertEqual(('alphonso.tv', 'LG (Alphonso)'), acr.endpoint('eu-acr3.alphonso.tv'))
        self.assertIsNone(acr.endpoint('notsamsungacr.com'))
        self.assertIsNone(acr.endpoint('samsungacr.com.example'))
        self.assertIsNone(acr.endpoint('tv'))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine()
        self.e.set_lists(allowlist=[], blocklist=[])

    def run_lookups(self, timestamps, domain=ACR_DOMAIN, device=TV):
        return [self.e.evaluate(domain, device, now_ms=ts) for ts in timestamps]

    def test_beaconing_acr_domain_is_blocked_with_the_measured_interval(self):
        decisions = self.run_lookups(timer(15 * S, jitter=0))
        self.assertTrue(all(d.action == 'flag' for d in decisions[:5]), 'not blocked before a timer is measured')
        self.assertEqual(('block', 'heuristic'), (decisions[-1].action, decisions[-1].source))
        self.assertEqual("ACR (Samsung): looked up every 15 s, likely sending what's on screen", decisions[-1].reason)

    def test_jittered_timer_reports_its_measured_mean(self):
        d = self.run_lookups(timer(30 * S, jitter=0.05))[-1]
        self.assertEqual('block', d.action)
        self.assertRegex(d.reason, r'every (29|30|31) s')

    def test_scattered_acr_lookups_are_not_blocked(self):
        decisions = self.run_lookups(scattered())
        self.assertFalse(any(d.action == 'block' for d in decisions))

    def test_non_acr_timer_is_still_only_flagged(self):
        d = self.run_lookups(timer(15 * S), domain='ping.unlisted.example')[-1]
        self.assertEqual(('flag', 'heuristic'), (d.action, d.source))

    def test_block_sticks_when_the_tv_retries_off_its_timer(self):
        ts = timer(15 * S, jitter=0)
        self.run_lookups(ts)
        retries = self.run_lookups(scattered(start=ts[-1] + 1 * S))
        self.assertTrue(all(d.action == 'block' for d in retries))
        self.assertTrue(all('every 15 s' in d.reason for d in retries))

    def test_per_device(self):
        self.run_lookups(timer(15 * S))
        self.assertEqual('flag', self.e.evaluate(ACR_DOMAIN, PHONE).action)

    def test_user_rule_and_allowlist_still_win(self):
        self.e.set_rules([Rule(1, '*.samsungacr.com', 'allow', TV)])
        d = self.run_lookups(timer(15 * S))[-1]
        self.assertEqual(('allow', 'rule'), (d.action, d.source))
        e = Engine()
        e.set_lists(allowlist=['samsungacr.com'], blocklist=[])
        self.assertEqual('allowlist', [e.evaluate(ACR_DOMAIN, TV, now_ms=t) for t in timer(15 * S)][-1].source)

    def test_blocklisted_acr_domain_keeps_its_blocklist_source(self):
        self.e.set_lists(allowlist=[], blocklist=['samsungacr.com'])
        self.assertEqual({'blocklist'}, {d.source for d in self.run_lookups(timer(15 * S))})

    def test_kept_blocks_are_bounded(self):
        with patch('eddie.decision.MAX_ACR', 2):
            for i in range(4):
                self.run_lookups(timer(15 * S, seed=i), domain=f'acr{i}.samsungacr.com')
        self.assertEqual([(TV, 'acr2.samsungacr.com'), (TV, 'acr3.samsungacr.com')], list(self.e.acr_caught))


def row(ts, domain=ACR_DOMAIN, action='block', beacon_s=15, device_mac=TV):
    return {'type': 'query', 'ts': ts, 'device_mac': device_mac, 'device': 'Living Room TV', 'domain': domain,
            'action': action, 'source': 'heuristic', 'reason': '', 'beacon_s': beacon_s}


class SightingTests(unittest.TestCase):
    def test_blocked_beacon_makes_one_event_with_the_measured_interval(self):
        s = Sightings()
        event = s.observe(row(1_000_000, beacon_s=17), lookups=9)
        self.assertEqual({'type': 'acr_detected', 'ts': 1_000_000, 'device_mac': TV, 'device': 'Living Room TV',
                          'domain': ACR_DOMAIN, 'acr_name': 'samsungacr.com', 'provider': 'Samsung',
                          'interval_s': 17, 'lookups': 9, 'source': 'heuristic',
                          'reason': "Living Room TV was checking in with Samsung's content recognition (ACR) server"
                                    " acr.samsungacr.com every 17 seconds. ACR is how TVs report what's on screen."
                                    " Blocked."}, event)
        self.assertIsNone(s.observe(row(1_000_000 + 15 * S)), 'once per device and domain per REPEAT_MS')
        self.assertIsNotNone(s.observe(row(1_000_000 + acr.REPEAT_MS)))
        self.assertIsNotNone(s.observe(row(1_000_000, device_mac=PHONE)))

    def test_no_measured_timer_no_block_no_event(self):
        s = Sightings()
        self.assertIsNone(s.observe(row(1, beacon_s=None)), 'never an invented interval')
        self.assertIsNone(s.observe(row(1, action='flag')), 'never says blocked without the DNS decision')
        self.assertIsNone(s.observe(row(1, action='allow')))
        self.assertIsNone(s.observe(row(1, domain='ping.unlisted.example')))

    def test_longer_timers_read_in_minutes(self):
        self.assertIn('about every 15 minutes', acr.reason('TV', ACR_DOMAIN, 'Samsung', 900))

    def test_memory_is_bounded(self):
        s = Sightings(max_recent=3)
        for i in range(10):
            s.observe(row(1_000_000 + i, domain=f'{i}.samsungacr.com'))
        self.assertEqual(3, len(s.last))


class IngestTests(unittest.TestCase):
    """Synthetic lookups go through the DNS decision engine, then /ingest, as the DNS server sends them."""

    def ingest(self, body):
        from fastapi.testclient import TestClient
        sent = []

        async def broadcast(msg):
            sent.append(msg)

        with patch.object(api, 'recent', []), patch.object(api, 'totals', {'seen': 0, 'blocked': 0}), \
                patch.dict('os.environ', {'EDDIE_DB': 'off'}), \
                patch.object(api, 'timing', {'beacons': Beacons(), 'idle': Idle()}), \
                patch.object(api, 'acr_sightings', Sightings()), patch.object(api, 'acr_recent', []) as kept, \
                patch.object(api, 'broadcast', broadcast):
            client = TestClient(api.app, client=('127.0.0.1', 50000))
            client.post('/ingest', json=body)
            with client.websocket_connect('/ws/events') as ws:
                snapshot = json.loads(ws.receive_text())
        return [m for m in sent if m['type'] == 'acr_detected'], kept, snapshot

    def through_dns(self, timestamps, engine=None, domain=ACR_DOMAIN):
        engine = engine or Engine()
        out = []
        for ts in timestamps:
            d = engine.evaluate(domain, TV, now_ms=ts)
            out.append({'ts': ts, 'device_mac': TV, 'device': 'Living Room TV', 'domain': domain,
                        'action': d.action, 'source': d.source, 'reason': d.reason})
        return out

    def test_tv_sending_every_15_seconds_gets_one_card(self):
        found, kept, snapshot = self.ingest(self.through_dns(timer(15 * S, n=20)))
        self.assertEqual(1, len(found))
        self.assertEqual(15, found[0]['interval_s'])
        self.assertEqual(beacons.MIN_GAPS + 1, found[0]['lookups'], 'sent on the first blocked lookup that measured a timer')
        self.assertTrue(found[0]['reason'].endswith('every 15 seconds. ACR is how TVs report what\'s on screen. Blocked.'))
        self.assertEqual(found, kept)
        self.assertEqual(found, snapshot['acr'])

    def test_blocklisted_acr_beacon_also_gets_a_card(self):
        e = Engine()
        e.set_lists(allowlist=[], blocklist=['alphonso.tv'])
        found, _, _ = self.ingest(self.through_dns(timer(20 * S), engine=e, domain='eu-acr3.alphonso.tv'))
        self.assertEqual([(20, 'blocklist', 'LG (Alphonso)')], [(f['interval_s'], f['source'], f['provider']) for f in found])

    def test_allowed_by_the_user_no_card(self):
        e = Engine()
        e.set_rules([Rule(1, '*.samsungacr.com', 'allow')])
        found, _, _ = self.ingest(self.through_dns(timer(15 * S), engine=e))
        self.assertEqual([], found)

    def test_scattered_acr_lookups_no_card(self):
        found, _, _ = self.ingest(self.through_dns(scattered()))
        self.assertEqual([], found)


if __name__ == '__main__':
    unittest.main()
