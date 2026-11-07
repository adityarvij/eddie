import sqlite3
import statistics
import time
import unittest

from eddie import store
from eddie.decision import UNKNOWN, Engine, Rule, Verdict

TV = 'aa:bb:cc:dd:ee:01'
PHONE = 'aa:bb:cc:dd:ee:02'


def decide(engine, domain, device=TV):
    d = engine.evaluate(domain, device)
    return d.action, d.source, d.reason


class DecisionOrderTests(unittest.TestCase):
    """One test per step of README "Decision order", each beating the next."""

    def setUp(self):
        self.e = Engine()
        self.e.set_lists(allowlist=['netflix.com'], blocklist=['netflix.com', 'tracker.example'])
        self.e.set_verdicts({
            'cdn.example': Verdict('needed', 0.9, 'video CDN'),
            'pixel.example': Verdict('tracker', 0.8, 'ad pixel'),
            'tracker.example': Verdict('needed', 0.9, 'never reached'),
        })

    def test_device_rule_beats_global_rule(self):
        self.e.set_rules([Rule(1, 'netflix.com', 'block'), Rule(2, 'netflix.com', 'allow', TV)])
        self.assertEqual(('allow', 'rule', 'device rule netflix.com'), decide(self.e, 'netflix.com'))
        self.assertEqual(('block', 'rule', 'global rule netflix.com'), decide(self.e, 'netflix.com', PHONE))

    def test_device_lockdown_blocks_all_but_its_rules_and_the_allowlist(self):
        self.e.set_rules([Rule(1, '*', 'block', TV), Rule(2, '*.shop.example', 'allow', TV),
                          Rule(3, 'news.example', 'allow'), Rule(4, 'netflix.com', 'block')])
        self.assertEqual(('allow', 'rule', 'device rule *.shop.example'), decide(self.e, 'a.shop.example'))
        self.assertEqual(('allow', 'allowlist', 'netflix.com'), decide(self.e, 'api.netflix.com'))
        self.assertEqual(('block', 'rule', 'device lockdown'), decide(self.e, 'news.example'))
        self.assertEqual(('block', 'rule', 'device lockdown'), decide(self.e, 'cdn.example'))
        self.assertEqual(('allow', 'rule', 'global rule news.example'), decide(self.e, 'news.example', PHONE))
        self.assertEqual('allow', self.e.evaluate('cdn.example', PHONE).action)

    def test_spoof_rule_passes_its_action_through(self):
        self.e.set_rules([Rule(1, '*.tracker.example', 'spoof'), Rule(2, 'netflix.com', 'spoof', TV)])
        self.assertEqual(('spoof', 'rule', 'global rule *.tracker.example'), decide(self.e, 'up.tracker.example'))
        self.assertEqual(('spoof', 'rule', 'device rule netflix.com'), decide(self.e, 'netflix.com'))
        self.assertEqual(('allow', 'allowlist', 'netflix.com'), decide(self.e, 'netflix.com', PHONE))

    def test_global_rule_beats_allowlist(self):
        self.e.set_rules([Rule(1, 'netflix.com', 'block')])
        self.assertEqual(('block', 'rule', 'global rule netflix.com'), decide(self.e, 'netflix.com'))

    def test_allowlist_beats_blocklist(self):
        self.assertEqual(('allow', 'allowlist', 'netflix.com'), decide(self.e, 'api.netflix.com'))

    def test_blocklist_beats_heuristic_and_ai(self):
        self.assertEqual(('block', 'blocklist', 'tracker.example'), decide(self.e, 'tracker.example'))

    def test_heuristic_beats_ai(self):
        self.e.set_verdicts({'metrics.example': Verdict('needed', 1.0)})
        self.assertEqual(('block', 'heuristic', 'keyword "metrics"'), decide(self.e, 'metrics.example'))

    def test_cached_ai_verdicts(self):
        self.e.set_verdicts({
            'cdn.example': Verdict('needed', 0.9, 'video CDN'),
            'pixel.example': Verdict('tracker', 0.8, 'ad pixel'),
            'maybe.example': Verdict('tracker', 0.69, 'weak guess'),
            'odd.example': Verdict('unsure', 0.9, 'no idea'),
        })
        self.assertEqual(('allow', 'ai', 'video CDN'), decide(self.e, 'cdn.example'))
        # The AI never blocks on its own, however confident it is.
        self.assertEqual(('flag', 'ai', 'likely tracker: ad pixel'), decide(self.e, 'pixel.example'))
        self.assertEqual(('flag', 'ai', 'likely tracker: weak guess'), decide(self.e, 'maybe.example'))
        self.assertEqual(('flag', 'ai', 'no idea'), decide(self.e, 'odd.example'))

    def test_unknown_is_flagged_for_the_classifier(self):
        self.assertEqual(('flag', 'default', UNKNOWN), decide(self.e, 'New.Example.'))

    def test_root_query_is_allowed(self):
        self.assertEqual('allow', self.e.evaluate('.', TV).action)


class RulePatternTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine()

    def test_exact_pattern_matches_only_that_domain(self):
        self.e.set_rules([Rule(1, 'video.example', 'block')])
        self.assertEqual('block', self.e.evaluate('VIDEO.example.', TV).action)
        self.assertEqual('flag', self.e.evaluate('a.video.example', TV).action)

    def test_wildcard_matches_domain_and_subdomains(self):
        self.e.set_rules([Rule(1, '*.amazon-adsystem.com', 'allow')])
        for domain in ('amazon-adsystem.com', 'aax-us-east.amazon-adsystem.com', 'a.b.amazon-adsystem.com'):
            self.assertEqual(('allow', 'rule', 'global rule *.amazon-adsystem.com'), decide(self.e, domain))
        # Not under the rule; the "adsystem" keyword blocks it instead.
        self.assertEqual(('block', 'heuristic'), decide(self.e, 'notamazon-adsystem.com')[:2])

    def test_most_specific_rule_wins(self):
        self.e.set_rules([Rule(1, '*.amazon.com', 'block'), Rule(2, '*.video.amazon.com', 'allow'),
                          Rule(3, 'x.video.amazon.com', 'block')])
        self.assertEqual('block', self.e.evaluate('www.amazon.com', TV).action)
        self.assertEqual('allow', self.e.evaluate('a.video.amazon.com', TV).action)
        self.assertEqual('block', self.e.evaluate('x.video.amazon.com', TV).action)

    def test_newest_rule_wins_a_tie(self):
        self.e.set_rules([Rule(2, 'shop.example', 'allow'), Rule(1, 'shop.example', 'block')])
        self.assertEqual('allow', self.e.evaluate('shop.example', TV).action)

    def test_device_rule_applies_only_to_that_device(self):
        self.e.set_rules([Rule(1, '*.shop.example', 'block', TV)])
        self.assertEqual('block', self.e.evaluate('shop.example', TV).action)
        self.assertEqual('flag', self.e.evaluate('shop.example', PHONE).action)

    def test_loads_rules_and_verdicts_from_store(self):
        conn = sqlite3.connect(':memory:')
        conn.executescript(store.SCHEMA)
        conn.executemany('INSERT INTO rules (scope, device_mac, pattern, action, created_by, ts) VALUES (?,?,?,?,?,?)', [
            ('global', None, '*.shop.example', 'block', 'voice', 1),
            ('device', TV, 'shop.example', 'allow', 'user', 2),
        ])
        conn.execute("INSERT INTO ai_verdicts VALUES ('cdn.example', 'needed', 0.9, NULL, 1)")
        self.e.load_from_store(conn)
        self.assertEqual(('allow', 'rule', 'device rule shop.example'), decide(self.e, 'shop.example'))
        self.assertEqual(('block', 'rule', 'global rule *.shop.example'), decide(self.e, 'shop.example', PHONE))
        self.assertEqual(('allow', 'ai', ''), decide(self.e, 'cdn.example'))


class ListTests(unittest.TestCase):
    def test_entries_cover_subdomains_only(self):
        e = Engine()
        e.set_lists(allowlist=[], blocklist=['Ads.Tracker.Example.'])
        self.assertEqual('block', e.evaluate('x.ads.tracker.example', TV).action)
        self.assertEqual('flag', e.evaluate('tracker.example', TV).action)
        self.assertEqual('flag', e.evaluate('notads.tracker.example', TV).action)


class HeuristicTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine()

    def test_blocks_tracker_keywords(self):
        cases = {
            'device-metrics-us.amazon.com': 'metrics',
            'telemetry.example.com': 'telemetry',
            'googleanalytics.example': 'analytics',
            'aax.amazon-adsystem.com': 'adsystem',
            'ads.roku.com': 'ads',
            'ads2.example.com': 'ads',
            'logs.roku.com': 'log',
            'app-log.example.com': 'log',
            'tracking.example.com': 'tracking',
            'beacon.example.com': 'beacon',
            'firebase-crashlytics.example': 'crashlytics',
        }
        for domain, kw in cases.items():
            self.assertEqual(('block', 'heuristic', f'keyword "{kw}"'), decide(self.e, domain), domain)

    def test_short_keywords_need_a_whole_word(self):
        for domain in ('uploads.example.com', 'blog.example.com', 'login.live.com', 'catalog.example.com',
                       'roads.example.com', 'dialog.example.com'):
            self.assertEqual('flag', self.e.evaluate(domain, TV).action, domain)

    def test_tld_is_ignored(self):
        self.assertEqual('flag', self.e.evaluate('example.ads', TV).action)


class HotPathTests(unittest.TestCase):
    def test_full_lists_stay_well_under_20_ms(self):
        e = Engine()
        e.set_lists(allowlist=[f'ok{i}.example' for i in range(500)],
                    blocklist=[f'host{i}.tracker{i % 1000}.example' for i in range(150_000)])
        e.set_rules([Rule(i, f'*.rule{i}.example', 'block', TV if i % 2 else None) for i in range(1000)])
        e.set_verdicts({f'seen{i}.example': Verdict('needed', 0.9) for i in range(10_000)})
        domains = ['a.b.c.d.unknown.example', 'host5.tracker5.example', 'x.rule7.example',
                   'seen9.example', 'device-metrics-us.amazon.com', 'ok3.example']
        timings = []
        for i in range(2000):
            start = time.perf_counter()
            e.evaluate(domains[i % len(domains)], TV)
            timings.append(time.perf_counter() - start)
        self.assertLess(statistics.median(timings), 0.001)
        self.assertLess(max(timings), 0.020)


if __name__ == '__main__':
    unittest.main()
