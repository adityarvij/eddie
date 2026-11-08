import os
import tempfile
import unittest
from collections import deque
from unittest.mock import patch
from datetime import datetime

import httpx2 as httpx

from eddie import demo_week, inferences, labels, store
from eddie.labels import lookup

MIN = 60 * 1000


def ev(ts, domain, action='allow', device='Fire Stick'):
    company, category = lookup(domain)
    return {'ts': ts, 'domain': domain, 'device': device, 'action': action, 'company': company, 'category': category}


class Facts(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime(2026, 11, 7, 23, 0).timestamp() * 1000)

    def test_empty_window_has_no_facts(self):
        self.assertEqual([], inferences.compute_facts([], now_ms=self.now))
        self.assertIn("don't have enough data", inferences.spoken_text([], 24))

    def test_facts_come_from_events(self):
        start = self.now - 3 * 60 * MIN
        events = [ev(start, 'device-metrics-us.amazon.com', 'block')]  # nobody watching yet
        events += [ev(start + 30 * MIN + i * MIN, 'nflxvideo.net') for i in range(3)]
        events += [ev(start + 31 * MIN, 'mads.amazon-adsystem.com', 'block')]
        events += [ev(start + 150 * MIN, 'nflxvideo.net')]
        facts = {f['fact']: f['evidence'] for f in inferences.compute_facts(events, now_ms=self.now)}
        self.assertEqual('2 tracking requests to Amazon in 24h, 2 blocked', facts['Amazon gets the most tracking data from this house'])
        self.assertIn('The Fire Stick reports home even when nobody is using it', facts)
        self.assertIn('no streaming for 1h', facts['The house may have been empty or asleep for a stretch'])
        self.assertNotIn('Someone is up watching after midnight', facts)

    def test_old_events_are_ignored(self):
        old = ev(self.now - 25 * 60 * MIN, 'device-metrics-us.amazon.com', 'block')
        self.assertEqual([], inferences.compute_facts([old], now_ms=self.now))


class Labels(unittest.TestCase):
    def test_new_topics_are_labelled_by_suffix(self):
        cases = {
            'www.youtubekids.com': ('YouTube Kids', 'kids'),
            'spclient.wg.spotify.com': ('Spotify', 'music'),
            'music.apple.com': ('Apple Music', 'music'),
            'www.apple.com': ('Apple', 'services'),
            'api.skybell.com': ('SkyBell', 'doorbell'),
            'api.ecobee.com': ('ecobee', 'thermostat'),
            'www.goodrx.com': ('GoodRx', 'health'),
            'secure.chase.com': ('Chase', 'finance'),
            'www.target.com': ('Target', 'shopping'),
            'app.slack.com': ('Slack', 'work'),
            'www.indeed.com': ('Indeed', 'jobs'),
            'www.nytimes.com': ('The New York Times', 'news'),
            'www.biblegateway.com': ('Bible Gateway', 'religion'),
            'ballotpedia.org': ('Ballotpedia', 'politics'),
            'avs-alexa-na.amazon.com': ('Amazon', 'assistant'),
            'idsync.rlcdn.com': ('LiveRamp', 'id-sync'),
        }
        for domain, want in cases.items():
            self.assertEqual(want, lookup(domain), domain)
        self.assertEqual('unknown', lookup('notcvs.com')[1], 'labels match whole labels, never substrings')

    def test_id_sync_counts_as_tracking(self):
        self.assertIn('id-sync', labels.TRACKING)

    def test_every_topic_is_a_label_category_and_report_category(self):
        used = {category for _, category in labels.DOMAINS.values()}
        for topic, (category, _) in inferences.TOPICS.items():
            self.assertIn(category, inferences.CATEGORIES)
            self.assertIn(topic, used | {'camera', 'alarm'}, topic)
        self.assertEqual(10, len(inferences.CATEGORIES))
        self.assertEqual({'Health', 'Beliefs', 'Kids'}, inferences.SENSITIVE)


class CategoryFacts(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime(2026, 11, 7, 23, 0).timestamp() * 1000)

    def facts(self, events):
        return {f['fact']: f for f in inferences.compute_facts(events, 48, now_ms=self.now)}

    def test_every_fact_has_a_category_and_sensitivity(self):
        start = self.now - 3 * 60 * MIN
        events = [ev(start, 'device-metrics-us.amazon.com', 'block')]
        events += [ev(start + m * MIN, 'nflxvideo.net') for m in (30, 31, 32, 150)]
        for f in inferences.compute_facts(events, now_ms=self.now):
            self.assertIn(f['category'], inferences.CATEGORIES)
            self.assertIs(f['sensitive'], f['category'] in inferences.SENSITIVE)
        with self.assertRaises(ValueError):
            inferences.fact('x', 'y', 0.5, 'Weather')

    def test_topic_fact_cites_lookups_companies_devices_and_days(self):
        day = 24 * 60 * MIN
        events = [ev(self.now - day - MIN, 'www.goodrx.com', device='Phone'),
                  ev(self.now - 2 * MIN, 'member.teladoc.com', device='Phone'),
                  ev(self.now - MIN, 'www.goodrx.com', device='Laptop')]
        f = self.facts(events)['Someone here uses health, pharmacy or telehealth services']
        self.assertEqual(('Health', True, 0.7), (f['category'], f['sensitive'], f['confidence']))
        self.assertEqual('3 lookups to GoodRx and Teladoc from Phone and Laptop on 2 days', f['evidence'])

    def test_one_day_is_less_sure_and_beliefs_are_sensitive(self):
        f = self.facts([ev(self.now - MIN, 'ballotpedia.org', device='Phone')])['Someone here visits political sites']
        self.assertEqual(('Beliefs', True, 0.5), (f['category'], f['sensitive'], f['confidence']))
        self.assertEqual('1 lookup to Ballotpedia from Phone on 1 day', f['evidence'])

    def test_unlabelled_traffic_makes_no_topic_fact(self):
        facts = self.facts([ev(self.now - MIN, 'example.org'), ev(self.now - 2 * MIN, 'example.net')])
        self.assertEqual([], [f for f in facts.values() if f['category'] not in ('Household', 'Safety')])

    def test_cross_device_linking_needs_two_devices(self):
        one = self.facts([ev(self.now - MIN, 'idsync.rlcdn.com', 'block', device='TV'),
                          ev(self.now - 2 * MIN, 'www.target.com', device='Phone')])
        self.assertFalse([k for k in one if 'can link' in k])
        self.assertEqual('2 differently named devices made lookups in 48h',
                         one['At least 2 devices are in use in this house']['evidence'])
        two = self.facts([ev(self.now - MIN, 'idsync.rlcdn.com', 'block', device='TV'),
                          ev(self.now - 2 * MIN, 'tapad.com', 'block', device='TV'),
                          ev(self.now - 3 * MIN, 'idsync.rlcdn.com', 'block', device='Phone')])
        f = two['LiveRamp can link 2 of your 2 devices']
        self.assertEqual(('Identity linking', False), (f['category'], f['sensitive']))
        self.assertEqual('tracking lookups to LiveRamp from Phone and TV', f['evidence'])
        self.assertIn('Ad companies are syncing IDs for this house', two)

    def test_single_device_has_no_household_fact(self):
        facts = self.facts([ev(self.now - MIN, 'www.target.com', device='Phone')])
        self.assertFalse([k for k in facts if 'devices are in use' in k])

    def test_beacon_to_an_acr_domain_is_whats_on_screen(self):
        events = [ev(self.now - 60 * MIN + i * 15_000, 'log-ingestion.samsungacr.com', 'block', device='TV')
                  for i in range(20)]
        [beacon] = [f for f in inferences.compute_facts(events, now_ms=self.now) if 'on a timer' in f['fact']]
        self.assertEqual("What's on screen", beacon['category'])


class Sessions(unittest.TestCase):
    def setUp(self):
        self.t0 = int(datetime(2026, 11, 2, 19, 0).timestamp() * 1000)  # a Monday

    def test_gap_under_ten_minutes_joins_a_session(self):
        events = [ev(self.t0 + m * MIN, 'nflxvideo.net') for m in (0, 9, 18)]
        [s] = inferences.sessions(events)
        self.assertEqual((s['category'], s['start'], s['end'], s['requests']), ('streaming', self.t0, self.t0 + 18 * MIN, 3))
        self.assertEqual(['Fire Stick'], s['devices'])

    def test_gap_of_ten_minutes_or_more_splits(self):
        events = [ev(self.t0, 'nflxvideo.net'), ev(self.t0 + 10 * MIN, 'nflxvideo.net')]
        self.assertEqual([1, 1], [s['requests'] for s in inferences.sessions(events)])

    def test_categories_are_separate_and_interleave(self):
        events = [ev(self.t0, 'nflxvideo.net'), ev(self.t0 + MIN, 'device-metrics-us.amazon.com'),
                  ev(self.t0 + 2 * MIN, 'nflxvideo.net', device='Tablet'), ev(self.t0 + 30 * MIN, 'device-metrics-us.amazon.com')]
        got = [(s['category'], s['requests'], s['devices']) for s in inferences.sessions(events)]
        self.assertEqual([('streaming', 2, ['Fire Stick', 'Tablet']), ('telemetry', 1, ['Fire Stick']),
                          ('telemetry', 1, ['Fire Stick'])], got)

    def test_unsorted_input_and_empty(self):
        self.assertEqual([], inferences.sessions([]))
        events = [ev(self.t0 + 5 * MIN, 'nflxvideo.net'), ev(self.t0, 'nflxvideo.net')]
        self.assertEqual(1, len(inferences.sessions(events)))


class WeekdayFacts(unittest.TestCase):
    def test_one_day_gives_no_weekday_fact(self):
        t0 = int(datetime(2026, 11, 2, 19, 0).timestamp() * 1000)
        s = inferences.sessions([ev(t0 + m * MIN, 'nflxvideo.net') for m in range(0, 60, 5)])
        self.assertEqual([], inferences.weekday_facts(s))

    def test_peak_day_and_weekend_skew_with_evidence(self):
        mon = int(datetime(2026, 11, 2, 19, 0).timestamp() * 1000)
        sat = int(datetime(2026, 11, 7, 9, 0).timestamp() * 1000)
        events = [ev(mon + m * MIN, 'nflxvideo.net') for m in range(0, 21, 5)]  # 20 min
        events += [ev(sat + m * MIN, 'nflxvideo.net') for m in range(0, 121, 5)]  # 120 min
        facts = {f['fact']: f for f in inferences.weekday_facts(inferences.sessions(events))}
        peak = facts['Streaming happens most on Saturdays']
        self.assertEqual('120 of 140 streaming minutes in 2 sessions were on Saturday, across 2 days with streaming', peak['evidence'])
        self.assertEqual(0.4, peak['confidence'])
        self.assertEqual('120 minutes a weekend day (1 day) vs 20 a weekday (1 day)',
                         facts['Streaming is heavier on weekends']['evidence'])


class DemoWeek(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime(2026, 11, 7, 23, 0).timestamp() * 1000)  # a Saturday night
        self.events = demo_week.events(self.now)

    def test_every_event_is_labelled_demo(self):
        self.assertTrue(self.events)
        for e in self.events:
            self.assertIs(e['demo'], True)
            self.assertTrue(e['device'].endswith('(demo)'))
            self.assertEqual('demo', e['source'])
        self.assertIn('Demo data', demo_week.LABEL)

    def test_seeded_and_bounded_to_a_week(self):
        self.assertEqual(self.events, demo_week.events(self.now))
        self.assertLessEqual(self.events[-1]['ts'], self.now)
        self.assertGreater(self.events[0]['ts'], self.now - 7 * 24 * 60 * MIN)

    def test_extra_devices_leave_the_original_week_alone(self):
        extras = {demo_week.PHONE, demo_week.LAPTOP, demo_week.SPEAKER, demo_week.THERMOSTAT, demo_week.DOORBELL}
        original = [e for e in self.events if e['device'] not in extras and e['domain'] not in
                    ('idsync.rlcdn.com', 'www.youtubekids.com')]
        with patch.object(demo_week, 'EXTRA_BACKGROUND', []), patch.object(demo_week, 'ROUTINE', []):
            self.assertEqual(original, demo_week.events(self.now))

    def test_report_covers_what_a_fire_stick_cannot_show(self):
        facts = inferences.compute_facts(self.events, 168, now_ms=self.now)
        self.assertEqual(set(inferences.CATEGORIES) - {"What's on screen"}, {f['category'] for f in facts})
        for f in facts:
            self.assertIs(f['sensitive'], f['category'] in ('Health', 'Beliefs', 'Kids'), f)

    def test_report_finds_the_scripted_patterns(self):
        facts = {f['fact']: f for f in inferences.compute_facts(self.events, 168, now_ms=self.now)}
        self.assertIn('Someone is up watching after midnight', facts)
        self.assertIn('The Living room TV (demo) reports home even when nobody is using it', facts)
        self.assertIn('Streaming is heavier on weekends', facts)
        for f in facts.values():
            self.assertTrue(f['evidence'])
            self.assertGreater(f['confidence'], 0)
            self.assertLessEqual(f['confidence'], 1)


class Endpoint(unittest.IsolatedAsyncioTestCase):
    async def test_demo_report_is_labelled_and_live_is_not(self):
        from eddie import api
        transport = httpx.ASGITransport(app=api.app)
        with patch.object(api, 'recent', deque()), patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            async with httpx.AsyncClient(transport=transport, base_url='http://eddie') as http:
                demo = (await http.get('/what-they-know', params={'demo': 'true'})).json()
                live = (await http.get('/what-they-know')).json()
        self.assertIs(demo['demo'], True)
        self.assertEqual(168, demo['hours'])
        self.assertTrue(demo['text'].startswith(demo_week.LABEL))
        self.assertTrue(demo['facts'])
        self.assertTrue({'Health', 'Beliefs', 'Kids'} <= {f['category'] for f in demo['facts'] if f['sensitive']})
        self.assertEqual({'demo': False, 'hours': 168, 'facts': []}, {k: live[k] for k in ('demo', 'hours', 'facts')})
        self.assertIs(live['enough_data'], False)
        self.assertFalse(api.recent, 'demo events must never enter the live feed')


class FromDatabase(unittest.IsolatedAsyncioTestCase):
    """The live report reads the DNS server's query log in eddie.db."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.conn = store.connect(self.path)
        self.addCleanup(self.conn.close)
        self.now = int(datetime.now().timestamp() * 1000)

    def log(self, rows):
        with self.conn:
            self.conn.executemany("INSERT INTO queries (ts, device_mac, domain, qtype, action, source, reason)"
                                  " VALUES (?, ?, ?, 'A', ?, 'blocklist', '')", rows)

    def test_rows_get_names_but_never_addresses(self):
        with self.conn:
            self.conn.executemany("INSERT INTO devices (mac, ip, hostname, vendor, label) VALUES (?, ?, ?, ?, ?)", [
                ('f0:4f:7c:11:22:33', '192.168.4.20', 'amazon-abc', 'Amazon Technologies Inc.', 'Fire Stick'),
                ('3c:22:fb:00:00:01', '192.168.4.21', 'Adis-iPhone', 'Apple, Inc.', None)])
        self.log([(self.now - 3 * MIN, 'f0:4f:7c:11:22:33', 'device-metrics-us.amazon.com', 'block'),
                  (self.now - 2 * MIN, '3c:22:fb:00:00:01', 'nflxvideo.net', 'allow'),
                  (self.now - MIN, '192.168.4.99', 'nflxvideo.net', 'allow'),
                  (self.now - 25 * 60 * MIN, 'f0:4f:7c:11:22:33', 'nflxvideo.net', 'allow')])  # too old
        events = inferences.events_from_db(self.conn, self.now - 60 * MIN)
        self.assertEqual(['Fire Stick', 'Apple, Inc. device', 'unlabeled device'], [e['device'] for e in events])
        self.assertEqual(('Amazon', 'telemetry', 'block'), (events[0]['company'], events[0]['category'], events[0]['action']))
        text = repr(events)
        for secret in ('f0:4f', '192.168', 'Adis-iPhone', 'amazon-abc'):
            self.assertNotIn(secret, text)

    async def get(self, feed=()):
        from eddie import api
        with patch.object(api, 'recent', deque(feed)), patch.dict(os.environ, {'EDDIE_DB': self.path}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie') as http:
                return (await http.get('/what-they-know')).json()

    async def test_endpoint_prefers_the_query_log(self):
        self.log([(self.now - MIN, 'f0:4f:7c:11:22:33', 'device-metrics-us.amazon.com', 'block')])
        report = await self.get(feed=[ev(self.now, 'doubleclick.net', 'block', device='Fake TV')])
        self.assertEqual('eddie.db', report['source'])
        self.assertEqual('1 tracking request to Amazon in 168h, 1 blocked', report['facts'][0]['evidence'])
        self.assertIs(report['enough_data'], False)

    async def test_requested_range_is_used_and_capped_to_retention(self):
        self.log([(self.now - 2 * MIN, 'f0:4f:7c:11:22:33', 'device-metrics-us.amazon.com', 'block'),
                  (self.now - MIN, 'f0:4f:7c:11:22:33', 'device-metrics-us.amazon.com', 'block')])
        from eddie import api
        with patch.dict(os.environ, {'EDDIE_DB': self.path, 'EDDIE_RETENTION_DAYS': '7'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie') as http:
                three_days = (await http.get('/what-they-know', params={'hours': 72})).json()
                month = (await http.get('/what-they-know', params={'hours': 720})).json()
        self.assertEqual((72, 72, False), (three_days['requested_hours'], three_days['hours'],
                                          three_days['limited_by_retention']))
        self.assertEqual((720, 168, True), (month['requested_hours'], month['hours'],
                                           month['limited_by_retention']))
        self.assertIn('only retain the last 1 week', month['text'])
        self.assertIs(month['enough_data'], True)

    async def test_empty_log_falls_back_to_the_feed(self):
        report = await self.get(feed=[ev(self.now - MIN, 'doubleclick.net', 'block', device='Fake TV')])
        self.assertEqual('feed', report['source'])
        self.assertIn('Google', report['facts'][0]['fact'])

    async def test_missing_database_falls_back_without_creating_it(self):
        os.remove(self.path)
        report = await self.get()
        self.assertEqual(('feed', []), (report['source'], report['facts']))
        self.assertFalse(os.path.exists(self.path))


if __name__ == '__main__':
    unittest.main()
