import random
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from eddie import api, beacons, classifier, inferences, store
from eddie.beacons import Beacons, Idle, interval_ms
from eddie.decision import Engine, Verdict
from eddie.labels import lookup

S = 1000
MIN = 60 * S
TV = 'aa:bb:cc:dd:ee:01'
START = 1_762_531_200_000


def regular(every_ms, n=10, jitter=0.0, start=START, seed=1):
    rng = random.Random(seed)
    ts = [start]
    for _ in range(n - 1):  # each gap is every_ms, plus or minus jitter
        ts.append(ts[-1] + round(every_ms * (1 + rng.uniform(-jitter, jitter))))
    return ts


def irregular(n=10, start=START, seed=2):
    rng = random.Random(seed)
    ts = [start]
    for _ in range(n - 1):
        ts.append(ts[-1] + rng.choice([5 * S, 40 * S, 3 * MIN, 12 * MIN]))
    return ts


def replay(timestamps, device=TV, domain='x.example'):
    b = Beacons()
    return [b.observe(device, domain, ts) for ts in timestamps][-1]


class IntervalTests(unittest.TestCase):
    def test_regular_30_and_60_second_timers_are_beacons(self):
        self.assertEqual(30 * S, interval_ms(regular(30 * S)))
        self.assertAlmostEqual(60 * S, interval_ms(regular(60 * S, jitter=0.05)), delta=2 * S)

    def test_irregular_gaps_are_not(self):
        self.assertIsNone(interval_ms(irregular()))
        self.assertIsNone(interval_ms(regular(60 * S, jitter=0.5)))

    def test_needs_enough_gaps_and_a_plausible_interval(self):
        self.assertIsNone(interval_ms(regular(60 * S, n=beacons.MIN_GAPS)))  # one gap short
        self.assertIsNotNone(interval_ms(regular(60 * S, n=beacons.MIN_GAPS + 1)))
        self.assertIsNone(interval_ms(regular(3 * S)))  # a burst, not a timer
        self.assertIsNone(interval_ms(regular(2 * 3600 * S)))


class TrackerTests(unittest.TestCase):
    def test_a_and_aaaa_pairs_count_as_one_lookup(self):
        ts = sorted(regular(60 * S) + [t + 30 for t in regular(60 * S)])
        self.assertEqual(60 * S, replay(ts))

    def test_pairs_are_per_device_and_domain(self):
        b = Beacons()
        for ts in regular(30 * S):
            b.observe(TV, 'a.example', ts)
        for ts in irregular():
            b.observe('phone', 'a.example', ts)
        self.assertEqual(30 * S, b.observe(TV, 'a.example', START + 10 * 30 * S))
        self.assertIsNone(b.observe('phone', 'a.example', START + 10 * 30 * S))
        self.assertIsNone(b.observe('other', 'a.example', START))

    def test_a_timer_that_stops_being_regular_is_dropped(self):
        b = Beacons()
        for ts in regular(30 * S):
            b.observe(TV, 'a.example', ts)
        last = regular(30 * S)[-1]
        self.assertIsNone([b.observe(TV, 'a.example', ts) for ts in irregular(start=last + 10 * MIN)][-1])

    def test_memory_is_bounded(self):
        b = Beacons(max_pairs=3)
        for i in range(10):
            b.observe(TV, f'{i}.example', START)
        self.assertEqual(3, len(b.seen))
        self.assertEqual([(TV, f'{i}.example') for i in (7, 8, 9)], list(b.seen))


class IdleTests(unittest.TestCase):
    def test_idle_after_five_minutes_without_media(self):
        idle = Idle()
        self.assertIsNone(idle.observe(TV, START, media=False))  # no media seen yet: unknown
        self.assertFalse(idle.observe(TV, START, media=True))
        self.assertFalse(idle.observe(TV, START + 4 * MIN, media=False))
        self.assertTrue(idle.observe(TV, START + 5 * MIN, media=False))
        self.assertFalse(idle.observe(TV, START + 6 * MIN, media=True))
        self.assertFalse(idle.observe(TV, START + 7 * MIN, media=False))
        self.assertIsNone(idle.observe('phone', START + 7 * MIN, media=False))


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine()
        self.e.set_lists(allowlist=['netflix.com'], blocklist=['tracker.example'])

    def last(self, domain, timestamps, device=TV):
        return [self.e.evaluate(domain, device, now_ms=ts) for ts in timestamps][-1]

    def test_beacon_on_no_list_is_flagged_as_heuristic(self):
        d = self.last('ping.unlisted.example', regular(60 * S, jitter=0.03))
        self.assertEqual(('flag', 'heuristic'), (d.action, d.source))
        self.assertEqual('looked up every 60 s, likely telemetry; waiting for classifier', d.reason)

    def test_irregular_unknown_stays_default(self):
        d = self.last('browse.unlisted.example', irregular())
        self.assertEqual(('flag', 'default'), (d.action, d.source))

    def test_lists_and_verdicts_come_first(self):
        self.e.set_verdicts({'cdn.example': Verdict('needed', 0.9, 'video CDN')})
        self.assertEqual('blocklist', self.last('tracker.example', regular(30 * S)).source)
        self.assertEqual('allowlist', self.last('api.netflix.com', regular(30 * S)).source)
        d = self.last('cdn.example', regular(30 * S))
        self.assertEqual(('allow', 'ai'), (d.action, d.source))

    def test_beacon_flags_stay_in_the_classifier_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(str(Path(tmp) / 'eddie.db'))
            now = int(time.time() * 1000)
            with conn:
                for domain, source in (('beacon.example', 'heuristic'), ('kw.example', 'heuristic')):
                    conn.execute("INSERT INTO queries (ts, device_mac, domain, qtype, action, source, reason)"
                                 " VALUES (?, ?, ?, 'A', ?, ?, '')",
                                 (now - 1000, TV, domain, 'flag' if domain == 'beacon.example' else 'block', source))
            self.assertEqual(['beacon.example'], classifier.candidates(conn, now, 10))
            conn.close()


def ev(ts, domain, action='block', device='Fire Stick'):
    company, category = lookup(domain)
    return {'ts': ts, 'domain': domain, 'device': device, 'action': action, 'company': company, 'category': category}


class WhatTheyKnowTests(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime(2026, 11, 7, 23, 0).timestamp() * 1000)

    def facts(self, events):
        return {f['fact']: f['evidence'] for f in inferences.compute_facts(events, now_ms=self.now)}

    def test_regular_unlisted_domain_surfaces(self):
        start = self.now - 30 * MIN
        events = [ev(ts, 'ping.unlisted.example', 'flag') for ts in regular(60 * S, n=20, start=start)]
        events += [ev(ts, 'browse.unlisted.example', 'flag') for ts in irregular(start=start)]
        facts = self.facts(events)
        self.assertEqual('15 lookups about every 60 seconds in 24h',
                         facts['The Fire Stick checks in with ping.unlisted.example on a timer'])
        self.assertFalse(any('browse.unlisted.example' in f for f in facts))

    def test_no_timer_no_fact(self):
        facts = self.facts([ev(ts, 'browse.unlisted.example', 'flag') for ts in irregular(start=self.now - 2 * 3600 * S)])
        self.assertFalse(any('on a timer' in f for f in facts))


class IngestTests(unittest.TestCase):
    def test_events_carry_idle_and_beacon_interval(self):
        from fastapi.testclient import TestClient
        start = int(time.time() * 1000) - 20 * MIN

        def query(ts, domain, action):
            return {'ts': ts, 'device_mac': TV, 'device': 'Fire Stick', 'domain': domain,
                    'action': action, 'source': 'default'}

        body = [query(start, 'nflxvideo.net', 'allow')]
        body += [query(ts, 'ping.unlisted.example', 'flag') for ts in regular(60 * S, n=8, start=start + 2 * S)]
        fresh = {'beacons': Beacons(), 'idle': Idle()}
        with patch.object(api, 'recent', []) as recent, patch.object(api, 'timing', fresh), \
                patch.object(api, 'totals', {'seen': 0, 'blocked': 0}):
            TestClient(api.app).post('/ingest', json=body)
        self.assertEqual(False, recent[0]['idle'])
        self.assertEqual(None, recent[0]['beacon_s'])
        self.assertEqual([False] * 5 + [True] * 3, [e['idle'] for e in recent[1:]])  # 5 min after the stream
        self.assertEqual([None] * 5 + [60] * 3, [e['beacon_s'] for e in recent[1:]])


if __name__ == '__main__':
    unittest.main()
