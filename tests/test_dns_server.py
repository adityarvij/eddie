import asyncio
import sqlite3
import statistics
import tempfile
import time
import unittest
from unittest.mock import patch

from dnslib import QTYPE, RCODE, RR, A, DNSRecord

from eddie import decision, dns_server, store

BLOCKED = 'device-metrics-us.amazon.com'  # blocked by the keyword heuristic
UNKNOWN = decision.UNKNOWN
REAL_IP = '93.184.216.34'


class FakeUpstream(asyncio.DatagramProtocol):
    """Answers every A query with REAL_IP, or stays silent when told to."""
    def __init__(self, silent=False):
        self.silent = silent
        self.seen = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.seen += 1
        if self.silent:
            return
        request = DNSRecord.parse(data)
        reply = request.reply()
        reply.add_answer(RR(request.q.qname, QTYPE.A, ttl=300, rdata=A(REAL_IP)))
        self.transport.sendto(reply.pack(), addr)


class Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.replies = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.replies.put_nowait(data)


class DNSServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        self.closers = []
        self.upstream_addr, self.upstream = await self.start_upstream()
        # A port nothing listens on, for "upstream is down".
        probe, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=('127.0.0.1', 0))
        self.dead_addr = probe.get_extra_info('sockname')
        probe.close()

    async def asyncTearDown(self):
        for close in self.closers:
            close()

    async def start_upstream(self, silent=False):
        transport, proto = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: FakeUpstream(silent), local_addr=('127.0.0.1', 0))
        self.closers.append(transport.close)
        return transport.get_extra_info('sockname'), proto

    async def start_server(self, upstreams, timeout=0.3, query_log=None):
        transport = await dns_server.serve('127.0.0.1', 0, upstreams, timeout, query_log)
        self.closers.append(transport.close)
        transport_c, client = await asyncio.get_running_loop().create_datagram_endpoint(
            Client, remote_addr=transport.get_extra_info('sockname'))
        self.closers.append(transport_c.close)
        return transport_c, client

    async def ask(self, conn, name, qtype='A', wait=2.0):
        transport, client = conn
        transport.sendto(DNSRecord.question(name, qtype).pack())
        return DNSRecord.parse(await asyncio.wait_for(client.replies.get(), wait))

    async def test_allowed_lookup_returns_upstream_answer(self):
        conn = await self.start_server([self.upstream_addr])
        reply = await self.ask(conn, 'google.com')
        self.assertEqual(RCODE.NOERROR, reply.header.rcode)
        self.assertEqual(REAL_IP, str(reply.rr[0].rdata))
        self.assertEqual(1, self.upstream.seen)

    async def test_blocked_lookup_is_sinkholed_without_upstream(self):
        conn = await self.start_server([self.upstream_addr])
        a = await self.ask(conn, BLOCKED)
        aaaa = await self.ask(conn, BLOCKED, 'AAAA')
        txt = await self.ask(conn, BLOCKED, 'TXT')
        self.assertEqual(('0.0.0.0', 60), (str(a.rr[0].rdata), a.rr[0].ttl))
        self.assertEqual(('::', 60), (str(aaaa.rr[0].rdata), aaaa.rr[0].ttl))
        self.assertEqual((RCODE.NOERROR, []), (txt.header.rcode, txt.rr))
        self.assertEqual(0, self.upstream.seen)

    async def test_blocked_answer_under_20ms(self):
        conn = await self.start_server([self.upstream_addr])
        times = []
        for _ in range(30):
            start = time.perf_counter()
            await self.ask(conn, BLOCKED)
            times.append((time.perf_counter() - start) * 1000)
        self.assertLess(statistics.median(times), 20)

    async def test_falls_back_to_second_upstream(self):
        _, silent = await self.start_upstream(silent=True)
        silent_addr = silent.transport.get_extra_info('sockname')
        for first in (self.dead_addr, silent_addr):
            conn = await self.start_server([first, self.upstream_addr])
            reply = await self.ask(conn, 'example.com')
            self.assertEqual(REAL_IP, str(reply.rr[0].rdata))

    async def test_servfail_when_every_upstream_fails(self):
        conn = await self.start_server([self.dead_addr])
        reply = await self.ask(conn, 'example.com')
        self.assertEqual(RCODE.SERVFAIL, reply.header.rcode)
        self.assertEqual([], reply.rr)

    async def test_garbage_is_dropped_and_server_keeps_working(self):
        conn = await self.start_server([self.upstream_addr])
        conn[0].sendto(b'not dns at all')
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(conn[1].replies.get(), 0.2)
        reply = await self.ask(conn, 'google.com')
        self.assertEqual(REAL_IP, str(reply.rr[0].rdata))

    async def test_decision_sees_normalized_domain_and_client(self):
        with patch('eddie.decision.evaluate', return_value=decision.Decision('allow', 'default', '')) as evaluate:
            conn = await self.start_server([self.upstream_addr])
            await self.ask(conn, 'Google.COM.')
        evaluate.assert_called_once_with('google.com', '127.0.0.1')

    async def test_every_lookup_writes_one_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = f'{tmp}/eddie.db'
            qlog = store.QueryLog(db)
            conn = await self.start_server([self.upstream_addr], query_log=qlog)
            await self.ask(conn, 'Google.com')
            await self.ask(conn, BLOCKED, 'AAAA')
            conn[0].sendto(b'not dns at all')  # not a lookup: no row
            dead = await self.start_server([self.dead_addr], query_log=qlog)
            await self.ask(dead, 'example.com')  # SERVFAIL still logged
            qlog.close()
            with sqlite3.connect(db) as c:
                rows = c.execute('SELECT device_mac, domain, qtype, action, source, reason FROM queries ORDER BY id').fetchall()
                ts = [r[0] for r in c.execute('SELECT ts FROM queries')]
        self.assertEqual([
            ('127.0.0.1', 'google.com', 'A', 'flag', 'default', UNKNOWN),
            ('127.0.0.1', BLOCKED, 'AAAA', 'block', 'heuristic', 'keyword "metrics"'),
            ('127.0.0.1', 'example.com', 'A', 'flag', 'default', UNKNOWN),
        ], rows)
        self.assertTrue(all(abs(t - time.time() * 1000) < 60_000 for t in ts))


class UpstreamConfigTests(unittest.TestCase):
    def test_parses_hosts_and_ports(self):
        with patch.dict('os.environ', {'EDDIE_UPSTREAMS': '9.9.9.9, 127.0.0.1:5300,'}):
            self.assertEqual([('9.9.9.9', 53), ('127.0.0.1', 5300)], dns_server.upstreams_from_env())


if __name__ == '__main__':
    unittest.main()
