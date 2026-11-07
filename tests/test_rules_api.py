import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import DNSRecord

from eddie import api, decision, dns_server, rules, store
from tests.test_dns_server import REAL_IP, Client, FakeUpstream

DOMAIN = 'news.example'  # unknown to every list: flagged and forwarded


class FakeSocket:
    """Stands in for a /ws/events client and keeps what the API broadcasts."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    def rule_changes(self):
        return [m for m in self.sent if m['type'] == 'rule_change']


class RulesAPITests(unittest.IsolatedAsyncioTestCase):
    """The rules API against a real DNS server, both reading one eddie.db."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}),
                  patch.object(decision, 'engine', decision.Engine()),
                  patch.dict(api.db, {'conn': None}),
                  patch.object(api, 'clients', set())):
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

    async def start_dns(self):
        """A DNS server with a fake upstream, reloading eddie.db like the real one."""
        loop = asyncio.get_running_loop()
        up, self.upstream = await loop.create_datagram_endpoint(FakeUpstream, local_addr=('127.0.0.1', 0))
        server = await dns_server.serve('127.0.0.1', 0, [up.get_extra_info('sockname')], 0.3)
        client, proto = await loop.create_datagram_endpoint(Client, remote_addr=server.get_extra_info('sockname'))
        watcher = dns_server.StoreWatcher(self.path, decision.engine)
        task = asyncio.create_task(dns_server.watch_store(watcher, every=0.02))
        self.closers += [up.close, server.close, client.close, task.cancel, watcher.conn.close]
        self.dns = client, proto
        await asyncio.sleep(0.05)  # first load

    async def ask(self, name):
        client, proto = self.dns
        client.sendto(DNSRecord.question(name).pack())
        reply = DNSRecord.parse(await asyncio.wait_for(proto.replies.get(), 2))
        return str(reply.rr[0].rdata)

    async def test_block_rule_blocks_the_next_query_and_delete_restores_it(self):
        await self.start_dns()
        self.assertEqual(REAL_IP, await self.ask(DOMAIN))

        r = await self.http.post('/rules', json={'pattern': DOMAIN, 'action': 'block', 'created_by': 'voice'})
        self.assertEqual(201, r.status_code)
        rule = r.json()
        self.assertTrue(rule['applied'])
        self.assertIsInstance(rule['id'], int)
        self.assertEqual((DOMAIN, 'block', 'global', None, 'voice'),
                         (rule['pattern'], rule['action'], rule['scope'], rule['device_mac'], rule['created_by']))
        self.assertEqual('0.0.0.0', await self.ask(DOMAIN))
        self.assertEqual(('block', 'rule'), (decision.evaluate(DOMAIN, '127.0.0.1').action,
                                             decision.evaluate(DOMAIN, '127.0.0.1').source))

        r = await self.http.delete(f"/rules/{rule['id']}")
        self.assertEqual(200, r.status_code)
        self.assertEqual({**{k: rule[k] for k in rules.COLUMNS}, 'deleted': True, 'applied': True}, r.json())
        self.assertEqual(REAL_IP, await self.ask(DOMAIN))
        self.assertEqual(('flag', 'default'), (decision.evaluate(DOMAIN, '127.0.0.1').action,
                                               decision.evaluate(DOMAIN, '127.0.0.1').source))

    async def test_device_rule_only_covers_that_device(self):
        await self.start_dns()
        r = await self.http.post('/rules', json={'pattern': '*.news.example', 'action': 'block', 'device_mac': '127.0.0.1'})
        self.assertEqual(('device', True), (r.json()['scope'], r.json()['applied']))
        self.assertEqual('0.0.0.0', await self.ask(DOMAIN))
        self.assertEqual('flag', decision.evaluate(DOMAIN, '172.20.10.5').action)

    async def test_allow_rule_overrides_a_heuristic_block(self):
        await self.start_dns()
        tracker = 'device-metrics-us.amazon.com'
        self.assertEqual('0.0.0.0', await self.ask(tracker))
        r = await self.http.post('/rules', json={'pattern': tracker, 'action': 'allow'})
        self.assertTrue(r.json()['applied'])
        self.assertEqual(REAL_IP, await self.ask(tracker))

    async def test_without_dns_the_rule_is_stored_but_not_acknowledged(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.1):
            r = await self.http.post('/rules', json={'pattern': DOMAIN, 'action': 'block'})
            self.assertEqual(201, r.status_code)
            self.assertFalse(r.json()['applied'])
            d = await self.http.delete(f"/rules/{r.json()['id']}")
            self.assertFalse(d.json()['applied'])

    async def test_list_is_newest_first(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0):
            for pattern in ('a.example', 'b.example'):
                await self.http.post('/rules', json={'pattern': pattern, 'action': 'block'})
        listed = (await self.http.get('/rules')).json()
        self.assertEqual(['b.example', 'a.example'], [r['pattern'] for r in listed])
        self.assertNotIn('applied', listed[0])

    async def test_pattern_is_normalized(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0):
            r = await self.http.post('/rules', json={'pattern': ' *.News.Example. ', 'action': 'block'})
        self.assertEqual('*.news.example', r.json()['pattern'])

    async def test_bad_requests_are_refused_and_nothing_is_stored(self):
        bad = [{'pattern': 'com', 'action': 'block'},
               {'pattern': '*.com', 'action': 'block'},
               {'pattern': 'a.com/path', 'action': 'block'},
               {'pattern': 'a.com', 'action': 'flag'},
               {'pattern': 'a.com', 'action': 'block', 'device_mac': 'tv; drop'},
               {'pattern': '', 'action': 'block'},
               {'pattern': '   ', 'action': 'block'},
               {'pattern': '*', 'action': 'block'},
               {'pattern': '*.', 'action': 'block'},
               {'pattern': '*.*', 'action': 'block'},
               {'pattern': '192.168.1.10', 'action': 'block'},
               {'pattern': '*.10.0.0.1', 'action': 'block'},
               {'pattern': '::1', 'action': 'block'},
               {'pattern': '2001:db8::1', 'action': 'block'},
               {'pattern': 'a.com', 'action': 'block', 'created_by': 'Voice Agent'},
               {'pattern': 'a.com', 'action': 'block', 'created_by': 'api'},
               {'pattern': 'a.com', 'action': 'block', 'created_by': 'dashboard'},
               {'pattern': 'a.com', 'action': 'block', 'created_by': None}]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(422, (await self.http.post('/rules', json=body)).status_code)
        self.assertEqual([], (await self.http.get('/rules')).json())
        self.assertEqual(0, rules.revision(api.rules_db()))
        self.assertEqual([], self.ws.rule_changes())

    async def test_created_by_is_user_voice_or_agent(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0):
            default = await self.http.post('/rules', json={'pattern': 'a.example', 'action': 'block'})
            self.assertEqual('user', default.json()['created_by'])
            for who in ('user', 'voice', 'agent'):
                r = await self.http.post('/rules', json={'pattern': 'a.example', 'action': 'block', 'created_by': who})
                self.assertEqual((201, who), (r.status_code, r.json()['created_by']))

    async def test_names_that_look_numeric_but_are_domains_are_accepted(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0):
            for pattern in ('1.1.1.example', '*.3m.com', '123.cdn.example'):
                with self.subTest(pattern=pattern):
                    self.assertEqual(201, (await self.http.post('/rules', json={'pattern': pattern, 'action': 'block'})).status_code)

    async def test_add_and_remove_publish_rule_change_without_claiming_enforcement(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0.05):  # no DNS server: stored, never applied
            r = await self.http.post('/rules', json={'pattern': DOMAIN, 'action': 'block',
                                                     'device_mac': '172.20.10.5', 'created_by': 'voice'})
            self.assertFalse(r.json()['applied'])
            rule = {k: r.json()[k] for k in rules.COLUMNS}
            added = {'type': 'rule_change', 'change': 'added', **rule}
            self.assertEqual([added], self.ws.rule_changes())
            self.assertEqual('172.20.10.5', added['device_mac'])

            await self.http.delete(f"/rules/{rule['id']}")
            self.assertEqual([added, {**added, 'change': 'removed'}], self.ws.rule_changes())
        for event in self.ws.rule_changes():
            self.assertFalse({'applied', 'enforced', 'blocked'} & event.keys())

    async def test_rule_change_is_sent_before_dns_acknowledges(self):
        await self.start_dns()
        with patch.object(api, 'wait_applied', side_effect=self.assert_already_announced):
            await self.http.post('/rules', json={'pattern': DOMAIN, 'action': 'block'})

    async def assert_already_announced(self, conn, rev):
        self.assertEqual(['added'], [e['change'] for e in self.ws.rule_changes()])
        return True

    async def test_deleting_a_missing_rule_is_404(self):
        self.assertEqual(404, (await self.http.delete('/rules/99')).status_code)
        self.assertEqual(0, rules.revision(api.rules_db()))
        self.assertEqual([], self.ws.rule_changes())

    async def test_rules_need_the_database(self):
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            self.assertEqual(503, (await self.http.get('/rules')).status_code)
            self.assertEqual(503, (await self.http.post('/rules', json={'pattern': 'a.com', 'action': 'block'})).status_code)


class WatcherAckTests(unittest.TestCase):
    def test_watcher_records_the_revision_it_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(os.path.join(tmp, 'eddie.db'))
            engine = decision.Engine()
            watcher = dns_server.StoreWatcher(os.path.join(tmp, 'eddie.db'), engine)
            watcher.poll()
            _, rev = rules.add(conn, rules.RuleIn(pattern=DOMAIN, action='block'))
            self.assertEqual(rev - 1, rules.loaded(conn))
            self.assertTrue(watcher.poll())
            self.assertEqual(rev, rules.loaded(conn))
            self.assertEqual('block', engine.evaluate(DOMAIN, 'x').action)
            self.assertFalse(watcher.poll())
            watcher.conn.close()
            conn.close()


if __name__ == '__main__':
    unittest.main()
