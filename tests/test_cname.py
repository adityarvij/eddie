import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from dnslib import CNAME, QTYPE, RR, A, DNSRecord

from eddie import decision, dns_server, store
from eddie.decision import CLOAKED, Engine, Rule, Verdict
from tests.test_dns_server import REAL_IP, ServerCase, upstream_answer

TV = 'aa:bb:cc:dd:ee:01'
ADOBE = 'pandora.net.ssl.sc.omtrdc.net'  # where smetrics.pandora.net really points (dig, 2026-11-08)
# smetrics itself is caught by the keyword heuristic, so tests use sstats, a name it doesn't catch.


def cloaked_answer(data, tcp=False):
    """sstats.* hosts CNAME to Adobe's tracker, cdn.* to an unlisted CDN; anything else as usual."""
    request = DNSRecord.parse(data)
    name = str(request.q.qname).rstrip('.')
    target = ADOBE if name.startswith('sstats.') else 'edge.cdn-unlisted.example' if name.startswith('cdn.') else None
    if not target:
        return upstream_answer(data, tcp)
    reply = request.reply()
    reply.add_answer(RR(request.q.qname, QTYPE.CNAME, ttl=300, rdata=CNAME(target + '.')))
    reply.add_answer(RR(target + '.', QTYPE.A, ttl=300, rdata=A(REAL_IP)))
    return reply.pack()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine()
        self.e.set_lists(allowlist=['netflix.com', 'ok.omtrdc.net'], blocklist=['omtrdc.net', 'netflix.com'])

    def decide(self, domain):
        d = self.e.evaluate(domain, TV)
        return d.action, d.source, d.reason

    def test_blocks_a_cname_to_a_blocklisted_name_and_remembers_it(self):
        self.assertEqual(('flag', 'default', decision.UNKNOWN), self.decide('sstats.pandora.net'))
        found = self.e.uncloak('sstats.pandora.net', ['Pandora.net.ssl.sc.omtrdc.net.'])
        self.assertEqual(decision.Decision('block', 'cname', CLOAKED.format('omtrdc.net')), found)
        self.assertEqual(('block', 'cname', CLOAKED.format('omtrdc.net')), self.decide('SStats.Pandora.net.'))

    def test_unlisted_or_allowlisted_targets_are_not_trackers(self):
        self.assertIsNone(self.e.uncloak('cdn.site.example', ['edge.cdn-unlisted.example']))
        self.assertIsNone(self.e.uncloak('pix.site.example', ['a.ok.omtrdc.net']))  # allowlist beats blocklist
        self.assertIsNone(self.e.uncloak('nothing.example', []))
        self.assertEqual('flag', self.decide('pix.site.example')[0])

    def test_rules_and_allowlist_still_win_over_a_remembered_cloak(self):
        self.e.uncloak('sstats.pandora.net', [ADOBE])
        self.e.uncloak('www.netflix.com', [ADOBE])
        self.e.set_rules([Rule(1, 'sstats.pandora.net', 'allow', TV)])
        self.assertEqual(('allow', 'rule', 'device rule sstats.pandora.net'), self.decide('sstats.pandora.net'))
        self.assertEqual(('allow', 'allowlist', 'netflix.com'), self.decide('www.netflix.com'))

    def test_a_remembered_cloak_beats_a_needed_verdict(self):
        self.e.set_verdicts({'sstats.pandora.net': Verdict('needed', 0.9, 'model guess')})
        self.e.uncloak('sstats.pandora.net', [ADOBE])
        self.assertEqual('cname', self.decide('sstats.pandora.net')[1])

    def test_new_lists_forget_old_cloaks(self):
        self.e.uncloak('sstats.pandora.net', [ADOBE])
        self.e.set_lists(allowlist=[], blocklist=[])
        self.assertEqual('default', self.decide('sstats.pandora.net')[1])

    def test_memory_is_bounded(self):
        with patch.object(decision, 'MAX_CLOAKED', 3):
            for i in range(5):
                self.e.uncloak(f'm{i}.site.example', [ADOBE])
        self.assertEqual(['m2.site.example', 'm3.site.example', 'm4.site.example'], list(self.e.cloaked))


class CnameTargetsTests(unittest.TestCase):
    def test_reads_cname_records_and_ignores_junk(self):
        query = DNSRecord.question('sstats.pandora.net').pack()
        self.assertEqual([ADOBE + '.'], dns_server.cname_targets(cloaked_answer(query)))
        self.assertEqual([], dns_server.cname_targets(upstream_answer(DNSRecord.question('a.example').pack())))
        self.assertEqual([], dns_server.cname_targets(b'not dns'))


class ServerTests(ServerCase):
    """The real server against a fake upstream that answers sstats.* with a CNAME to Adobe."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        lists = (decision.engine.allowlist, decision.engine.blocklist)
        decision.engine.set_lists(allowlist=['allowed.example'], blocklist=['omtrdc.net'])
        self.addCleanup(decision.engine.set_lists, *lists)
        patcher = patch('tests.test_dns_server.upstream_answer', cloaked_answer)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_first_answer_is_blocked_logged_once_and_later_lookups_skip_upstream(self):
        with tempfile.TemporaryDirectory() as tmp:
            qlog = store.QueryLog(f'{tmp}/eddie.db')
            conn = await self.start_server([self.upstream_addr], query_log=qlog)
            first = await self.ask(conn, 'sstats.shop1.example')
            again = await self.ask(conn, 'sstats.shop1.example')
            fine = await self.ask(conn, 'cdn.shop1.example')
            qlog.close()
            with sqlite3.connect(f'{tmp}/eddie.db') as c:
                rows = c.execute('SELECT domain, action, source, reason FROM queries ORDER BY id').fetchall()
        self.assertEqual(['0.0.0.0', '0.0.0.0'], [str(first.rr[0].rdata), str(again.rr[0].rdata)])
        self.assertEqual(REAL_IP, str(fine.rr[-1].rdata))
        self.assertEqual(2, self.upstream.seen)  # the repeat never left Eddie
        reason = CLOAKED.format('omtrdc.net')
        self.assertEqual([('sstats.shop1.example', 'block', 'cname', reason),
                          ('sstats.shop1.example', 'block', 'cname', reason),
                          ('cdn.shop1.example', 'flag', 'default', decision.UNKNOWN)], rows)

    async def test_feed_gets_the_final_decision(self):
        published = []

        class Feed:
            def publish(self, event):
                published.append(event)

        conn = await self.start_server([self.upstream_addr], feed=Feed())
        await self.ask(conn, 'sstats.shop2.example')
        self.assertEqual([('block', 'cname')], [(e['action'], e['source']) for e in published])

    async def test_allowlisted_domains_and_allow_rules_are_not_uncloaked(self):
        conn = await self.start_server([self.upstream_addr])
        listed = await self.ask(conn, 'sstats.allowed.example')
        allowed = decision.Decision('allow', 'rule', 'global rule sstats.shop3.example')
        with patch('eddie.decision.evaluate', return_value=allowed):
            ruled = await self.ask(conn, 'sstats.shop3.example')
        self.assertEqual([REAL_IP, REAL_IP], [str(listed.rr[-1].rdata), str(ruled.rr[-1].rdata)])
        self.assertNotIn('sstats.shop3.example', decision.engine.cloaked)

    async def test_tcp_answers_are_uncloaked_too(self):
        await self.start_server([self.upstream_addr])
        reply = await self.ask_tcp(await self.connect(), 'sstats.shop4.example')
        self.assertEqual('0.0.0.0', str(reply.rr[0].rdata))
        self.assertEqual(1, self.upstream.seen_tcp)


if __name__ == '__main__':
    unittest.main()
