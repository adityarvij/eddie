import os
import unittest
from collections import deque
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx2 as httpx

from eddie import demo_week, occupancy
from eddie.labels import lookup

MIN = 60 * 1000
HOUR = 60 * MIN


def ev(ts, domain, device='Fire Stick', action='allow'):
    company, category = lookup(domain)
    return {'ts': ts, 'domain': domain, 'device': device, 'action': action, 'company': company, 'category': category}


def house(first: datetime, days: int, busy=lambda t: 19 <= t.hour < 22, now=None):
    """Hourly traffic from first for days: telemetry every 10 min, streaming every 2 min while busy(t)."""
    out = []
    for h in range(days * 24):
        t = first + timedelta(hours=h)
        ts = int(t.timestamp() * 1000)
        out += [ev(ts + m * MIN, 'device-metrics-us.amazon.com', action='block') for m in range(0, 60, 10)]
        if busy(t):
            out += [ev(ts + m * MIN, 'nflxvideo.net') for m in range(0, 60, 2)]
    return [e for e in out if now is None or e['ts'] <= now]


class Report(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime(2026, 11, 7, 23, 59).timestamp() * 1000)  # a Saturday
        self.monday = datetime(2026, 11, 2)

    def test_full_week_says_this_week_and_finds_the_weekday_pattern(self):
        r = occupancy.report(house(datetime(2026, 11, 1), 7, now=self.now), 168, self.now)
        self.assertTrue(r['enough'] and r['full_range'])
        self.assertTrue(r['text'].startswith("From this week's data, someone could tell the house was empty"))
        [weekdays, weekends] = r['empty_times']
        self.assertEqual('on weekdays from 6 AM to 7 PM and from 10 PM to midnight', weekdays['when'])
        self.assertEqual('no streaming in those hours on 5 of the 5 weekdays Eddie saw', weekdays['evidence'])
        self.assertIn('weekend days', weekends['evidence'])

    def test_short_history_is_reported_as_recent_data(self):
        r = occupancy.report(house(datetime(2026, 11, 5), 3, now=self.now), 168, self.now)
        self.assertFalse(r['full_range'])
        self.assertEqual(72, r['covered_hours'])
        self.assertTrue(r['text'].startswith('From recent data (3 days, not the full week), someone could tell'))

    def test_a_covered_shorter_range_names_that_range(self):
        r = occupancy.report(house(datetime(2026, 11, 1), 7, now=self.now), 48, self.now)
        self.assertTrue(r['full_range'])
        self.assertTrue(r['text'].startswith('From the last 2 days, someone could tell'))
        self.assertTrue(all(e['pattern'] for e in r['empty_times']))

    def test_one_day_is_not_called_a_pattern(self):
        r = occupancy.report(house(datetime(2026, 11, 1), 7, now=self.now), 24, self.now)
        self.assertEqual(['on Sat from 6 AM to 7 PM and from 10 PM to midnight'], [e['when'] for e in r['empty_times']])
        self.assertFalse(r['empty_times'][0]['pattern'])
        self.assertIn("That's under 2 days of data, so it's not a pattern yet.", r['text'])

    def test_no_traffic_says_so(self):
        r = occupancy.report([], 168, self.now)
        self.assertEqual((False, 0, []), (r['enough'], r['covered_hours'], r['devices']))
        self.assertEqual("I haven't seen any traffic this week, so I can't tell when the house was empty.", r['text'])
        self.assertIn("in the last 3 days", occupancy.report([], 72, self.now)['text'])

    def test_a_few_hours_is_not_enough(self):
        r = occupancy.report(house(datetime(2026, 11, 7, 20), 1, now=self.now), 168, self.now)
        self.assertFalse(r['enough'])
        self.assertEqual("I've only seen 4 hours of traffic this week, not enough to tell when the house was empty.", r['text'])

    def test_no_streaming_means_no_conclusion(self):
        r = occupancy.report(house(self.monday, 5, busy=lambda t: False, now=self.now), 168, self.now)
        self.assertFalse(r['enough'])
        self.assertIn("nobody streamed anything, so I can't tell home from away", r['text'])
        self.assertEqual([], r['empty_times'])

    def test_streaming_every_hour_finds_no_empty_time(self):
        r = occupancy.report(house(self.monday, 3, busy=lambda t: True, now=self.now), 168, self.now)
        self.assertTrue(r['enough'])
        self.assertEqual([], r['empty_times'])
        self.assertIn("I didn't find 2 or more waking hours in a row that looked empty", r['text'])

    def test_hours_without_any_lookup_are_not_quiet(self):
        # Eddie was off 9 AM to 5 PM every day: those hours prove nothing, so no empty stretch.
        events = [e for e in house(self.monday, 3, busy=lambda t: t.hour >= 6, now=self.now)
                  if not 9 <= datetime.fromtimestamp(e['ts'] / 1000).hour < 17]
        r = occupancy.report(events, 168, self.now)
        self.assertEqual([], r['empty_times'])


class Devices(unittest.TestCase):
    def setUp(self):
        self.first = datetime(2026, 11, 2)
        self.now = int(datetime(2026, 11, 4, 23, 59).timestamp() * 1000)

    def test_ranked_by_leak_score_and_security_flagged(self):
        events = house(self.first, 3, now=self.now)
        for h in range(3 * 24):  # a doorbell that checks in steadily, and a speaker with a few lookups
            ts = int((self.first + timedelta(hours=h)).timestamp() * 1000)
            events.append(ev(ts, 'fw.ring.com', device='Front doorbell'))
        events += [ev(int(self.first.timestamp() * 1000) + i * HOUR, 'pool.ntp.org', device='Speaker') for i in range(3)]
        r = occupancy.report(events, 168, self.now)
        rows = {d['device']: d for d in r['devices']}
        self.assertEqual(['Fire Stick', 'Front doorbell', 'Speaker'], [d['device'] for d in r['devices']])
        self.assertGreater(rows['Fire Stick']['leak_score'], 50)
        self.assertEqual(0, rows['Front doorbell']['leak_score'])
        self.assertIsNone(rows['Speaker']['leak_score'])
        self.assertTrue(rows['Front doorbell']['security'] and rows['Front doorbell']['never_block'])
        self.assertFalse(rows['Fire Stick']['never_block'])
        self.assertIn('The Front doorbell is a security device, so keep it online: never block it.', r['text'])
        self.assertIn(f"The Fire Stick has an occupancy leak score of {rows['Fire Stick']['leak_score']} out of 100", r['text'])

    def test_score_numbers_come_from_the_counts(self):
        r = occupancy.report(house(self.first, 3, now=self.now), 168, self.now)
        [tv] = r['devices']
        # busy hours: 6 telemetry + 30 streaming lookups; quiet hours: 6 telemetry.
        self.assertEqual((36.0, 6.0, round(100 * 30 / 42)), (tv['per_busy_hour'], tv['per_quiet_hour'], tv['leak_score']))
        self.assertEqual(sum(1 for e in house(self.first, 3, now=self.now) if e['action'] == 'block'), tv['blocked'])


class DemoWeek(unittest.TestCase):
    def test_demo_week_report(self):
        now = int(datetime(2026, 11, 7, 23, 0).timestamp() * 1000)
        r = occupancy.report(demo_week.events(now), 168, now)
        self.assertTrue(r['full_range'] and r['enough'])
        self.assertIn('on weekdays from 6 AM to 6 PM', r['text'])
        self.assertEqual({demo_week.TV, demo_week.TABLET}, {d['device'] for d in r['devices']})


class Endpoint(unittest.IsolatedAsyncioTestCase):
    async def test_demo_is_labelled_and_empty_live_says_not_enough(self):
        from eddie import api
        with patch.object(api, 'recent', deque()), patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie') as http:
                demo = (await http.get('/burglars-eye', params={'demo': 'true'})).json()
                live = (await http.get('/burglars-eye', params={'hours': 72})).json()
                default = (await http.get('/burglars-eye')).json()
                with patch.dict(os.environ, {'EDDIE_RETENTION_DAYS': '7'}):
                    month = (await http.get('/burglars-eye', params={'hours': 720})).json()
                bad = await http.get('/burglars-eye', params={'hours': 9000})
        self.assertIs(demo['demo'], True)
        self.assertTrue(demo['text'].startswith(demo_week.LABEL))
        self.assertTrue(demo['devices'])
        self.assertEqual({'demo': False, 'source': 'feed', 'hours': 72, 'enough': False},
                         {k: live[k] for k in ('demo', 'source', 'hours', 'enough')})
        self.assertIn("haven't seen any traffic in the last 3 days", live['text'])
        self.assertEqual((168, False), (default['hours'], default['limited_by_retention']))
        self.assertEqual((720, 168, True), (month['requested_hours'], month['hours'], month['limited_by_retention']))
        self.assertTrue(month['text'].startswith('I only retain the last 1 week, so I used all available data. '))
        self.assertEqual(422, bad.status_code)
        self.assertFalse(api.recent, 'demo events must never enter the live feed')
