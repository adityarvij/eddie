import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx2 as httpx
from dnslib import QTYPE, RCODE, RR, A, DNSRecord

from eddie import api, decision, dns_server, offline
from tests.test_dns_server import BLOCKED, REAL_IP, Client, FakeUpstream, watch


def upstream_answer(name, ttl=300):
    request = DNSRecord.question(name)
    reply = request.reply()
    reply.add_answer(RR(request.q.qname, QTYPE.A, ttl=ttl, rdata=A(REAL_IP)))
    return request, reply.pack()


class CacheTests(unittest.TestCase):
    def test_answer_takes_the_request_id_and_question_and_ages_ttls(self):
        mode = offline.Mode()
        request, answer = upstream_answer('example.com')
        mode.put(request, answer, now=100.0)
        again = DNSRecord.question('ExAmple.COM')
        reply = DNSRecord.parse(mode.answer(again, now=160.0))
        self.assertEqual(again.header.id, reply.header.id)
        self.assertEqual('ExAmple.COM.', str(reply.q.qname))
        self.assertEqual((REAL_IP, 240), (str(reply.rr[0].rdata), reply.rr[0].ttl))

    def test_expired_answer_is_served_stale_until_max_age(self):
        mode = offline.Mode(max_age=3600)
        request, answer = upstream_answer('example.com', ttl=60)
        mode.put(request, answer, now=0.0)
        self.assertEqual(offline.STALE_TTL, DNSRecord.parse(mode.answer(request, now=600.0)).rr[0].ttl)
        self.assertIsNone(mode.answer(request, now=3601.0))
        self.assertEqual({}, mode.cache)

    def test_misses_other_types_servfail_and_evicts_oldest(self):
        mode = offline.Mode(max_entries=2)
        request, answer = upstream_answer('a.example')
        mode.put(request, answer)
        self.assertIsNone(mode.answer(DNSRecord.question('a.example', 'AAAA')))
        failed = request.reply()
        failed.header.rcode = RCODE.SERVFAIL
        mode.put(DNSRecord.question('b.example'), failed.pack())
        self.assertIsNone(mode.answer(DNSRecord.question('b.example')))
        for name in ('c.example', 'd.example'):
            mode.put(*upstream_answer(name))
        self.assertIsNone(mode.answer(request))
        self.assertIsNotNone(mode.answer(DNSRecord.question('d.example')))


class OfflineDNSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        self.closers = []
        patcher = patch.object(dns_server, 'offline_mode', offline.Mode())
        patcher.start()
        self.closers.append(patcher.stop)
        up, self.upstream = await loop.create_datagram_endpoint(FakeUpstream, local_addr=('127.0.0.1', 0))
        server = await dns_server.serve('127.0.0.1', 0, [up.get_extra_info('sockname')], 0.3)
        client, self.client = await loop.create_datagram_endpoint(Client, remote_addr=server.get_extra_info('sockname'))
        self.closers += [up.close, server.close, client.close]
        self.send = client.sendto

    async def asyncTearDown(self):
        for close in reversed(self.closers):
            close()

    async def ask(self, name):
        self.send(DNSRecord.question(name).pack())
        return DNSRecord.parse(await asyncio.wait_for(self.client.replies.get(), 2))

    async def test_offline_never_forwards_but_answers_cached_and_blocked(self):
        self.assertEqual(REAL_IP, str((await self.ask('google.com')).rr[0].rdata))
        self.assertEqual(1, self.upstream.seen)

        dns_server.offline_mode.on = True
        self.assertEqual(REAL_IP, str((await self.ask('google.com')).rr[0].rdata))
        self.assertEqual(RCODE.SERVFAIL, (await self.ask('never-seen.example')).header.rcode)
        self.assertEqual('0.0.0.0', str((await self.ask(BLOCKED)).rr[0].rdata))
        self.assertEqual(1, self.upstream.seen)

        dns_server.offline_mode.on = False
        self.assertEqual(REAL_IP, str((await self.ask('never-seen.example')).rr[0].rdata))
        self.assertEqual(2, self.upstream.seen)


class OfflineAPITests(unittest.IsolatedAsyncioTestCase):
    """POST /offline-mode against a real DNS server, both reading one eddie.db."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'eddie.db')
        self.closers = [self.tmp.cleanup]
        self.mode = offline.Mode()
        for p in (patch.dict(os.environ, {'EDDIE_DB': self.path}),
                  patch.object(decision, 'engine', decision.Engine()),
                  patch.object(dns_server, 'offline_mode', self.mode),
                  patch.dict(api.db, {'conn': None})):
            p.start()
            self.closers.append(p.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://eddie')

    async def asyncTearDown(self):
        await self.http.aclose()
        if api.db['conn']:
            api.db['conn'].close()
        for close in reversed(self.closers):
            close()

    async def start_dns(self):
        loop = asyncio.get_running_loop()
        up, self.upstream = await loop.create_datagram_endpoint(FakeUpstream, local_addr=('127.0.0.1', 0))
        server = await dns_server.serve('127.0.0.1', 0, [up.get_extra_info('sockname')], 0.3)
        client, proto = await loop.create_datagram_endpoint(Client, remote_addr=server.get_extra_info('sockname'))
        watcher = dns_server.StoreWatcher(self.path, decision.engine)
        stop_watching = watch(watcher, 0.02)
        self.closers += [up.close, server.close, client.close, stop_watching]
        self.dns = client, proto
        await asyncio.sleep(0.05)  # first load

    async def ask(self, name):
        client, proto = self.dns
        client.sendto(DNSRecord.question(name).pack())
        return DNSRecord.parse(await asyncio.wait_for(proto.replies.get(), 2))

    async def modes(self):
        """GET /mode without away mode's fields (tests/test_away.py)."""
        body = (await self.http.get('/mode')).json()
        return {k: body[k] for k in ('home_mode', 'offline', 'offline_applied')}

    async def test_toggle_is_acknowledged_by_dns_and_shown_in_mode(self):
        await self.start_dns()
        self.assertEqual({'home_mode': False, 'offline': False, 'offline_applied': True},
                         await self.modes())

        r = await self.http.post('/offline-mode', json={'offline': True})
        self.assertEqual({'offline': True, 'applied': True}, r.json())
        self.assertTrue(self.mode.on)
        self.assertEqual({'home_mode': False, 'offline': True, 'offline_applied': True},
                         await self.modes())
        self.assertEqual(RCODE.SERVFAIL, (await self.ask('news.example')).header.rcode)
        self.assertEqual(0, self.upstream.seen)

        r = await self.http.post('/offline-mode', json={'offline': False})
        self.assertEqual({'offline': False, 'applied': True}, r.json())
        self.assertEqual(REAL_IP, str((await self.ask('news.example')).rr[0].rdata))
        self.assertEqual(1, self.upstream.seen)

    async def test_without_dns_the_toggle_is_stored_but_not_acknowledged(self):
        class Socket:
            def __init__(self):
                self.sent = []

            async def send_text(self, data):
                self.sent.append(json.loads(data))

        ws = Socket()
        with patch.object(api, 'APPLY_TIMEOUT', 0.1), patch.object(api, 'clients', {ws}):
            r = await self.http.post('/offline-mode', json={'offline': True})
        self.assertEqual({'offline': True, 'applied': False}, r.json())
        self.assertEqual([{'type': 'offline_mode', 'offline': True, 'applied': False}], ws.sent)
        self.assertEqual({'home_mode': False, 'offline': True, 'offline_applied': False},
                         await self.modes())

    async def test_dns_restart_loads_the_stored_state(self):
        with patch.object(api, 'APPLY_TIMEOUT', 0):
            await self.http.post('/offline-mode', json={'offline': True})
        watcher = dns_server.StoreWatcher(self.path, decision.Engine(), self.mode)
        self.assertTrue(watcher.poll())
        watcher.conn.close()
        self.assertTrue(self.mode.on)
        self.assertTrue((await self.http.get('/mode')).json()['offline_applied'])

    async def test_needs_the_database(self):
        with patch.dict(os.environ, {'EDDIE_DB': 'off'}):
            self.assertEqual(503, (await self.http.post('/offline-mode', json={'offline': True})).status_code)
            self.assertEqual({'home_mode': False, 'offline': False, 'offline_applied': False},
                             await self.modes())
        self.assertEqual(422, (await self.http.post('/offline-mode', json={'offline': 'maybe'})).status_code)


if __name__ == '__main__':
    unittest.main()
