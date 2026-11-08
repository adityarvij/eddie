import json
import unittest
from unittest.mock import patch

from eddie import api, bypass
from eddie.bypass import Suspicions
from eddie.decision import Engine, Rule

TV = 'aa:bb:cc:dd:ee:01'
PHONE = 'aa:bb:cc:dd:ee:02'


def query(domain, ts=1_000_000, device_mac=TV, device='Fire Stick', action='allow'):
    return {'ts': ts, 'device_mac': device_mac, 'device': device, 'domain': domain,
            'action': action, 'source': 'default'}


class ResolverTests(unittest.TestCase):
    def test_names_and_their_subdomains_match(self):
        self.assertEqual(('dns.google', 'Google Public DNS'), bypass.resolver('dns.google'))
        self.assertEqual(('dns.google', 'Google Public DNS'), bypass.resolver('DNS64.dns.google.'))
        self.assertEqual(('dns.nextdns.io', 'NextDNS'), bypass.resolver('abc123.dns.nextdns.io'))
        self.assertEqual(('cloudflare-dns.com', 'Cloudflare'), bypass.resolver('mozilla.cloudflare-dns.com'))

    def test_other_names_dont(self):
        for domain in ('google.com', 'google', 'nextdns.io', 'quad9.net', 'notdns.google.com', 'dns.google.evil.example'):
            self.assertIsNone(bypass.resolver(domain), domain)


class SuspicionTests(unittest.TestCase):
    def test_a_resolver_lookup_names_the_device(self):
        event = Suspicions().observe(query('dns.google'))
        self.assertEqual({'type': 'bypass_suspected', 'ts': 1_000_000, 'device_mac': TV, 'device': 'Fire Stick',
                          'domain': 'dns.google', 'resolver': 'Google Public DNS', 'blocked': False},
                         {k: v for k, v in event.items() if k != 'reason'})
        self.assertIn('Fire Stick looked up dns.google', event['reason'])
        self.assertIn('may be sending', event['reason'])

    def test_other_lookups_are_ignored(self):
        self.assertIsNone(Suspicions().observe(query('netflix.com')))

    def test_once_per_device_and_name_per_window(self):
        s = Suspicions(repeat_ms=1000)
        self.assertIsNotNone(s.observe(query('dns.google', ts=0)))
        self.assertIsNone(s.observe(query('dns64.dns.google', ts=500)))  # same name entry
        self.assertIsNotNone(s.observe(query('dns.google', ts=500, device_mac=PHONE)))
        self.assertIsNotNone(s.observe(query('cloudflare-dns.com', ts=500)))
        self.assertIsNotNone(s.observe(query('dns.google', ts=1000)))

    def test_blocked_says_it_only_stops_bootstrapping(self):
        event = Suspicions().observe(query('dns.google', action='block'))
        self.assertTrue(event['blocked'])
        self.assertIn('only stops encrypted DNS set up by name', event['reason'])
        self.assertIn("resolver's IP still skips Eddie", event['reason'])

    def test_never_claims_coverage(self):
        for action in ('allow', 'block', 'flag'):
            reason = Suspicions().observe(query('dns.google', action=action))['reason'].lower()
            for claim in ('covered', 'protected', 'fully', 'all lookups'):
                self.assertNotIn(claim, reason)

    def test_memory_is_bounded(self):
        s = Suspicions(repeat_ms=10, max_recent=3)
        for i in range(10):
            s.observe(query('dns.google', ts=i, device_mac=f'ip:10.0.0.{i}'))
        self.assertLessEqual(len(s.last), 3)


class BlockRuleTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine()
        self.e.set_lists(allowlist=['doh.pub'], blocklist=[])

    def test_off_by_default(self):
        self.assertFalse(self.e.block_doh)
        self.assertEqual('flag', self.e.evaluate('dns.google', TV).action)

    def test_on_blocks_resolver_names(self):
        self.e.block_doh = True
        d = self.e.evaluate('dns64.dns.google', TV)
        self.assertEqual(('block', 'blocklist', 'encrypted DNS resolver dns.google'), (d.action, d.source, d.reason))
        self.assertEqual('flag', self.e.evaluate('google.com', TV).action)

    def test_rules_and_allowlist_still_win(self):
        self.e.block_doh = True
        self.e.set_rules([Rule(1, 'dns.google', 'allow', TV)])
        self.assertEqual('allow', self.e.evaluate('dns.google', TV).action)
        self.assertEqual('block', self.e.evaluate('dns.google', PHONE).action)
        self.assertEqual('allow', self.e.evaluate('doh.pub', TV).action)


class IngestTests(unittest.TestCase):
    def ingest(self, body):
        from fastapi.testclient import TestClient
        sent = []

        async def broadcast(msg):
            sent.append(msg)

        with patch.object(api, 'recent', []), patch.object(api, 'totals', {'seen': 0, 'blocked': 0}), \
                patch.object(api, 'suspicions', Suspicions()), patch.object(api, 'bypass_recent', []) as kept, \
                patch.object(api, 'broadcast', broadcast):
            client = TestClient(api.app, client=('127.0.0.1', 50000))
            client.post('/ingest', json=body)
            with client.websocket_connect('/ws/events') as ws:
                snapshot = json.loads(ws.receive_text())
        return sent, kept, snapshot

    def test_resolver_lookup_pushes_bypass_suspected_after_the_query(self):
        sent, kept, snapshot = self.ingest([query('dns.google'), query('dns.google', ts=1_000_500), query('a.example')])
        types = [m['type'] for m in sent]
        self.assertEqual(['query', 'bypass_suspected', 'query', 'query', 'totals'], types)
        self.assertEqual('Fire Stick', sent[1]['device'])
        self.assertEqual([sent[1]], kept)
        self.assertEqual([sent[1]], snapshot['bypass'])
        self.assertEqual(3, snapshot['totals']['seen'], 'a suspicion is not a lookup')


if __name__ == '__main__':
    unittest.main()
