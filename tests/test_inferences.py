import unittest
from datetime import datetime

from eddie import inferences
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
        self.assertIn("haven't seen enough", inferences.spoken_text([], 24))

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


if __name__ == '__main__':
    unittest.main()
