import asyncio
import errno
import sqlite3
import statistics
import tempfile
import time
import unittest
from unittest.mock import patch

from dnslib import EDNS0, QTYPE, RCODE, RR, TXT, A, DNSRecord

from eddie import decision, dns_server, store

BLOCKED = 'device-metrics-us.amazon.com'  # blocked by the keyword heuristic
UNKNOWN = decision.UNKNOWN
REAL_IP = '93.184.216.34'
BIG_TXT = ['v=' + 'x' * 250, 'k=' + 'y' * 250, 'p=' + 'z' * 250]  # about 760 bytes: too big for UDP


def upstream_answer(data, tcp=False):
    """A for REAL_IP; TXT for BIG_TXT, truncated over UDP like a real resolver."""
    request = DNSRecord.parse(data)
    reply = request.reply()
    if request.q.qtype == QTYPE.TXT:
        if not tcp:
            reply.header.tc = 1
        else:
            for text in BIG_TXT:
                reply.add_answer(RR(request.q.qname, QTYPE.TXT, ttl=300, rdata=TXT(text)))
    else:
        reply.add_answer(RR(request.q.qname, QTYPE.A, ttl=300, rdata=A(REAL_IP)))
    return reply.pack()


class FakeUpstream(asyncio.DatagramProtocol):
    """Answers over UDP and, on the same port, TCP; or stays silent when told to."""
    def __init__(self, silent=False):
        self.silent = silent
        self.seen = 0
        self.seen_tcp = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.seen += 1
        if not self.silent:
            self.transport.sendto(upstream_answer(data), addr)

    async def handle_tcp(self, reader, writer):
        try:
            while True:
                data = await dns_server.read_message(reader)
                self.seen_tcp += 1
                if not self.silent:
                    writer.write(dns_server.frame(upstream_answer(data, tcp=True)))
        except EOFError:
            pass
        finally:
            writer.close()


class Client(asyncio.DatagramProtocol):
    def __init__(self):
        self.replies = asyncio.Queue()

    def datagram_received(self, data, addr):
        self.replies.put_nowait(data)


class ServerCase(unittest.IsolatedAsyncioTestCase):
    """A fake upstream, and helpers to start Eddie and ask it over UDP or TCP."""

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

    async def on_one_port(self, start_udp, start_tcp, tries=20):
        """Bind UDP on a free port, then TCP on the same number.

        The OS only promises the UDP port is free; the same TCP port can
        already be held (say, by another test's client), so try a fresh one.
        """
        for attempt in range(tries):
            transport, proto = await start_udp()
            addr = transport.get_extra_info('sockname')
            try:
                tcp = await start_tcp(proto, addr)
            except OSError as e:
                transport.close()
                if e.errno != errno.EADDRINUSE or attempt == tries - 1:
                    raise
                continue
            self.closers += [transport.close, tcp.close]
            return transport, proto, addr

    async def start_upstream(self, silent=False):
        loop = asyncio.get_running_loop()
        _, proto, addr = await self.on_one_port(
            lambda: loop.create_datagram_endpoint(lambda: FakeUpstream(silent), local_addr=('127.0.0.1', 0)),
            lambda proto, addr: asyncio.start_server(proto.handle_tcp, *addr))
        return addr, proto

    async def start_server(self, upstreams, timeout=0.3, query_log=None, feed=None, idle=1.0):
        """UDP and TCP on one port, like the real server. The UDP client; TCP clients come from connect()."""
        async def start_udp():
            return await dns_server.serve('127.0.0.1', 0, upstreams, timeout, query_log, feed), None
        _, _, self.server_addr = await self.on_one_port(
            start_udp,
            lambda _, addr: dns_server.serve_tcp(*addr, upstreams, timeout, query_log, feed, idle=idle))
        transport_c, client = await asyncio.get_running_loop().create_datagram_endpoint(
            Client, remote_addr=self.server_addr)
        self.closers.append(transport_c.close)
        return transport_c, client

    async def connect(self):
        reader, writer = await asyncio.open_connection(*self.server_addr)
        self.closers.append(writer.close)
        return reader, writer

    async def ask(self, conn, name, qtype='A', wait=2.0, request=None):
        transport, client = conn
        transport.sendto((request or DNSRecord.question(name, qtype)).pack())
        return DNSRecord.parse(await asyncio.wait_for(client.replies.get(), wait))

    async def ask_tcp(self, stream, name, qtype='A', wait=2.0):
        reader, writer = stream
        writer.write(dns_server.frame(DNSRecord.question(name, qtype).pack()))
        return DNSRecord.parse(await asyncio.wait_for(dns_server.read_message(reader), wait))


class DNSServerTests(ServerCase):
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

    async def test_spoofed_lookup_answers_eddies_ip_without_upstream(self):
        spoofed = decision.Decision('spoof', 'rule', 'global rule t.example')
        with patch('eddie.decision.evaluate', return_value=spoofed), patch.dict(dns_server.spoof, ip='192.168.4.1'):
            conn = await self.start_server([self.upstream_addr])
            a = await self.ask(conn, 't.example')
            aaaa = await self.ask(conn, 't.example', 'AAAA')
        self.assertEqual(('192.168.4.1', 60), (str(a.rr[0].rdata), a.rr[0].ttl))
        self.assertEqual((RCODE.NOERROR, []), (aaaa.header.rcode, aaaa.rr))  # no IPv6 answer: clients use the A
        self.assertEqual(0, self.upstream.seen)

    async def test_spoof_without_eddies_ip_is_blocked(self):
        spoofed = decision.Decision('spoof', 'rule', 'global rule t.example')
        with patch('eddie.decision.evaluate', return_value=spoofed), patch.dict(dns_server.spoof, ip=None):
            conn = await self.start_server([self.upstream_addr])
            a = await self.ask(conn, 't.example')
        self.assertEqual('0.0.0.0', str(a.rr[0].rdata))
        self.assertEqual(0, self.upstream.seen)

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
        evaluate.assert_called_once()
        self.assertEqual(('google.com', '127.0.0.1', '127.0.0.1'), evaluate.call_args.args[:3])  # no device map: key is the IP

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


class TCPTests(ServerCase):
    """The same lookups over TCP, where devices retry an answer too big for UDP."""

    async def test_tcp_allowed_lookup_is_forwarded_over_tcp(self):
        await self.start_server([self.upstream_addr])
        reply = await self.ask_tcp(await self.connect(), 'google.com')
        self.assertEqual(REAL_IP, str(reply.rr[0].rdata))
        self.assertEqual((0, 1), (self.upstream.seen, self.upstream.seen_tcp))

    async def test_tcp_blocked_lookup_is_sinkholed_without_upstream(self):
        await self.start_server([self.upstream_addr])
        stream = await self.connect()
        a = await self.ask_tcp(stream, BLOCKED)  # several queries on one connection
        aaaa = await self.ask_tcp(stream, BLOCKED, 'AAAA')
        txt = await self.ask_tcp(stream, BLOCKED, 'TXT')
        self.assertEqual(('0.0.0.0', 60), (str(a.rr[0].rdata), a.rr[0].ttl))
        self.assertEqual(('::', 60), (str(aaaa.rr[0].rdata), aaaa.rr[0].ttl))
        self.assertEqual((RCODE.NOERROR, []), (txt.header.rcode, txt.rr))
        self.assertEqual((0, 0), (self.upstream.seen, self.upstream.seen_tcp))

    async def test_truncated_udp_answer_resolves_on_tcp_retry(self):
        conn = await self.start_server([self.upstream_addr])
        udp = await self.ask(conn, 'big.example', 'TXT')
        self.assertEqual((1, []), (udp.header.tc, udp.rr))
        tcp = await self.ask_tcp(await self.connect(), 'big.example', 'TXT')
        self.assertEqual(0, tcp.header.tc)
        self.assertEqual(BIG_TXT, [str(rr.rdata).strip('"') for rr in tcp.rr])
        self.assertGreater(len(tcp.pack()), 512)

    async def test_tcp_servfail_when_every_upstream_fails(self):
        await self.start_server([self.dead_addr])
        reply = await self.ask_tcp(await self.connect(), 'example.com')
        self.assertEqual(RCODE.SERVFAIL, reply.header.rcode)

    async def test_tcp_lookup_goes_through_decision_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = f'{tmp}/eddie.db'
            qlog = store.QueryLog(db)
            with patch('eddie.decision.evaluate', wraps=decision.evaluate) as evaluate:
                await self.start_server([self.upstream_addr], query_log=qlog)
                stream = await self.connect()
                await self.ask_tcp(stream, 'Google.COM.')
                await self.ask_tcp(stream, BLOCKED)
            qlog.close()
            with sqlite3.connect(db) as c:
                rows = c.execute('SELECT device_mac, domain, action FROM queries ORDER BY id').fetchall()
        self.assertEqual(('google.com', '127.0.0.1', '127.0.0.1'), evaluate.call_args_list[0].args[:3])
        self.assertEqual([('127.0.0.1', 'google.com', 'flag'), ('127.0.0.1', BLOCKED, 'block')], rows)

    async def test_idle_tcp_connection_is_closed(self):
        await self.start_server([self.upstream_addr], idle=0.2)
        reader, _ = await self.connect()
        self.assertEqual(b'', await asyncio.wait_for(reader.read(), 2))

    async def test_half_sent_query_is_closed_after_timeout(self):
        await self.start_server([self.upstream_addr], idle=0.2)
        reader, writer = await self.connect()
        writer.write(b'\x00\x40abc')  # promises 64 bytes, sends 3
        self.assertEqual(b'', await asyncio.wait_for(reader.read(), 2))

    async def test_garbage_over_tcp_closes_only_that_connection(self):
        await self.start_server([self.upstream_addr])
        reader, writer = await self.connect()
        writer.write(dns_server.frame(b'not dns at all'))
        self.assertEqual(b'', await asyncio.wait_for(reader.read(), 2))
        reply = await self.ask_tcp(await self.connect(), 'google.com')
        self.assertEqual(REAL_IP, str(reply.rr[0].rdata))

    async def test_connections_over_the_limit_are_refused(self):
        with patch.object(dns_server, 'MAX_TCP_CONNECTIONS', 2):
            await self.start_server([self.upstream_addr])
            first, second = await self.connect(), await self.connect()
            await self.ask_tcp(first, 'google.com')
            await self.ask_tcp(second, 'google.com')
            extra, _ = await self.connect()
            self.assertEqual(b'', await asyncio.wait_for(extra.read(), 2))
            first[1].close()
            await asyncio.sleep(0.05)
            reply = await self.ask_tcp(await self.connect(), 'google.com')
        self.assertEqual(REAL_IP, str(reply.rr[0].rdata))

    async def test_offline_answer_too_big_for_udp_is_truncated(self):
        mode = dns_server.offline.Mode()
        with patch.object(dns_server, 'offline_mode', mode):
            conn = await self.start_server([self.upstream_addr])
            await self.ask_tcp(await self.connect(), 'big.example', 'TXT')  # cached while online
            mode.on = True
            udp = await self.ask(conn, 'big.example', 'TXT')
            edns = DNSRecord.question('big.example', 'TXT')
            edns.add_ar(EDNS0(udp_len=1232))
            udp_edns = await self.ask(conn, None, request=edns)
            tcp = await self.ask_tcp(await self.connect(), 'big.example', 'TXT')
        self.assertEqual((1, []), (udp.header.tc, udp.rr))
        self.assertEqual((0, 3), (udp_edns.header.tc, len(udp_edns.rr)))
        self.assertEqual(3, len(tcp.rr))


class UpstreamConfigTests(unittest.TestCase):
    def test_parses_hosts_and_ports(self):
        with patch.dict('os.environ', {'EDDIE_UPSTREAMS': '9.9.9.9, 127.0.0.1:5300,'}):
            self.assertEqual([('9.9.9.9', 53), ('127.0.0.1', 5300)], dns_server.upstreams_from_env())


if __name__ == '__main__':
    unittest.main()

    def test_spoof_ip_is_an_ipv4_address_or_off(self):
        for value, want in (('', None), (' 192.168.4.1 ', '192.168.4.1')):
            with patch.dict('os.environ', {'EDDIE_SPOOF_IP': value}):
                self.assertEqual(want, dns_server.spoof_ip_from_env())
        for value in ('eddie.local', '::1', '300.1.1.1'):
            with patch.dict('os.environ', {'EDDIE_SPOOF_IP': value}), self.assertRaises(ValueError):
                dns_server.spoof_ip_from_env()
