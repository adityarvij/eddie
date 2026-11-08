import json
import math
import time
import unittest
from pathlib import Path

from eddie import classifier, decision, domain_model

GOLDEN = json.loads((Path(__file__).parent / 'fixtures' / 'domain_golden.json').read_text())


class FeatureTests(unittest.TestCase):
    def test_splits_subdomain_words_from_the_registered_domain(self):
        feats = domain_model.features('Device-Metrics-US.amazon.com.')
        for f in ('s:com', 'd:amazon.com', 'f:device-metrics-us', 'w:metrics', 'dw:amazon', 'n:1'):
            self.assertIn(f, feats)
        self.assertEqual(len(feats), len(set(feats)))

    def test_private_suffixes_count_as_the_suffix(self):
        self.assertIn('s:cloudfront.net', domain_model.features('d1abc2xyz.cloudfront.net'))


class ShippedModelTests(unittest.TestCase):
    model = domain_model.default()

    def test_loads_with_sane_thresholds_and_finite_weights(self):
        self.assertLess(self.model.needed_at, 0.5)
        self.assertGreater(self.model.tracker_at, 0.5)
        self.assertTrue(all(math.isfinite(w) for w in self.model.weights.values()))

    def test_never_calls_well_known_needed_hosts_trackers(self):
        wrong = [h for h in GOLDEN['needed'] if self.model.verdict(h)['label'] == 'tracker']
        self.assertEqual([], wrong)

    def test_never_waves_through_tracker_named_hosts_it_never_saw(self):
        labels = {h: self.model.verdict(h)['label'] for h in GOLDEN['tracker']}
        self.assertNotIn('needed', labels.values(), labels)
        # Generic words (events, collector, click) are common on unlisted hosts too, so those stay unsure.
        self.assertGreaterEqual(list(labels.values()).count('tracker'), 4, labels)

    def test_golden_trackers_dodge_the_keyword_heuristic(self):
        for host in GOLDEN['tracker']:
            self.assertIsNone(decision.keyword(host), host)

    def test_bare_site_names_are_never_trackers(self):
        for host in ('vendably.shop', 'www.vendably.shop', 'adserver.com'):
            self.assertNotEqual('tracker', self.model.verdict(host)['label'], host)

    def test_every_verdict_meets_the_classifier_contract(self):
        hosts = GOLDEN['needed'] + GOLDEN['tracker'] + [
            'x.y', 'a' * 63 + '.com', '123.456.example', 'xn--bcher-kva.example', 'some_host.local-ish.net']
        for host in hosts:
            self.assertIsNotNone(classifier.parse_verdict(classifier.ask_model({'domain': host})), host)

    def test_tracker_reason_names_only_words_from_the_host(self):
        verdict = self.model.verdict('adsync.brightnest-tv.com')
        words = verdict['reason'].partition(': ')[2].split(', ') if ': ' in verdict['reason'] else []
        for word in words:
            self.assertIn(word, 'adsync.brightnest-tv.com')

    def test_scores_fast_enough_for_the_queue(self):
        hosts = GOLDEN['needed'] * 20
        started = time.perf_counter()
        for host in hosts:
            self.model.score(host)
        self.assertLess((time.perf_counter() - started) / len(hosts), 0.002)


if __name__ == '__main__':
    unittest.main()
