"""Eddie's DNS server: answers every lookup from devices pointed at Eddie.

Allowed lookups are forwarded to an upstream resolver (1.1.1.1, then 9.9.9.9)
and the answer is passed back untouched. Blocked lookups get a dead end:
0.0.0.0 for A, :: for AAAA, an empty answer for anything else, TTL 60.
Spoofed lookups (rule action spoof, #99) get Eddie's own IPv4 (EDDIE_SPOOF_IP)
for A and an empty answer for anything else, so the device talks to the stub
server (spoof_server.py) instead. Without EDDIE_SPOOF_IP they're blocked.
The decision comes from decision.evaluate, which never waits on a model.

Every lookup is written to the queries table in eddie.db (EDDIE_DB) through
store.QueryLog, which queues the row and writes it off the event loop.
EDDIE_DB=off logs nothing; EDDIE_RETENTION_DAYS (default 7) bounds history.
The blocklists (blocklists/*.txt) and essentials allowlist (allowlist.txt)
are loaded once at startup (lists.py; EDDIE_BLOCKLISTS, EDDIE_ALLOWLIST).
Rules and AI verdicts written to eddie.db by other processes (rules API,
classifier) are reloaded into the decision engine within RELOAD_EVERY, and
the rules revision loaded is written back so the rules API can acknowledge it.
devices.DeviceMap maps each client IP to its MAC from the ARP table, read in a
worker thread, and records the devices seen in eddie.db (#14). A MAC seen for
the first time is sent to the API as a new_device alert, and quarantine
(#98, quarantine.py) is reloaded like rules.
Offline mode (#26, offline.py) is loaded the same way: while it's on, nothing
is forwarded and allowed lookups get the answer kept from the last time they
were forwarded, or SERVFAIL. Away mode (#109, away.py) is loaded with the
rules: the devices it covers and its end time go into the decision engine.
So is home mode (#20, home.py): while it's on, indoor camera upload domains are blocked.
Spoof mode changes blocklist, cloaked-tracker and tracker-heuristic blocks into
spoof replies after its stored revision is loaded; explicit and safety rules stay blocks.
EDDIE_BLOCK_DOH=on blocks the encrypted DNS resolver names in bypass.py (#97).
Each decision is also sent to the live feed (POST /ingest on the local API)
by feed.FeedPublisher, off the reply path; EDDIE_FEED=off turns that off.
The same port also takes DNS over TCP (RFC 7766), where devices retry an
answer too big for UDP: the same decision and logging, forwarded upstream
over TCP. A UDP reply that won't fit the client's buffer (an offline answer
first fetched over TCP) is sent truncated, so the device retries over TCP.
CNAME uncloaking: a lookup the engine let through on its own judgement (not
a rule or the allowlist) whose answer has a CNAME to a blocklisted name is a
tracker hiding behind the site's own domain (smetrics.brand.com -> Adobe's
omtrdc.net). It gets the block answer instead, logged with source cname, and
decision.uncloak remembers the domain. Each lookup is logged once, after its
answer, so the log and feed show what the device actually got.
Geo alerts (#102, geoip.py): each forwarded answer is queued for a worker
thread that looks its addresses up in the pinned offline country database and
sends a geo_alert when a device starts using a new country; EDDIE_GEOIP=off.

Run (port 53):            sudo .venv/bin/python -m eddie.dns_server
Development (no sudo):    EDDIE_DNS_PORT=8053 uv run python -m eddie.dns_server
                          dig @127.0.0.1 -p 8053 google.com
                          dig @127.0.0.1 -p 8053 +tcp google.com
"""
import asyncio
import logging
import os
import signal
import sqlite3
import threading
import time

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSRecord

from . import away, decision, devices, geoip, home, lists, nft, offline, quarantine, rules, sensitivity, spoof_mode
from . import store
from .feed import FeedPublisher, feed_from_env, query_event
from .store import QueryLog, query_log_from_env

BLOCK_TTL = 60
UPSTREAM_TIMEOUT = 2.0  # seconds per upstream before trying the next one
RELOAD_EVERY = 0.2  # seconds between checks of eddie.db for new rules or verdicts
MAX_IN_FLIGHT = 512  # lookups being answered at once; extra packets are dropped
MAX_TCP_CONNECTIONS = 64  # open TCP clients; extra connections are closed at once
TCP_IDLE_TIMEOUT = 10.0  # seconds a TCP client may take to send its next query (RFC 7766 suggests ~10)
UDP_MAX = 512  # largest UDP reply for a client that doesn't say otherwise with EDNS

log = logging.getLogger("eddie.dns")
offline_mode = offline.Mode()
spoof = {"ip": None}  # Eddie's LAN IPv4 for spoof answers (EDDIE_SPOOF_IP); None blocks instead
geo = {"watch": None}  # geoip.GeoWatch for geo alerts (#102); None looks nothing up


def upstreams_from_env() -> list[tuple[str, int]]:
    """EDDIE_UPSTREAMS="1.1.1.1,9.9.9.9:53" (IPv4, optional port)."""
    out = []
    for item in os.environ.get("EDDIE_UPSTREAMS", "1.1.1.1,9.9.9.9").split(","):
        host, _, port = item.strip().partition(":")
        if host:
            out.append((host, int(port or 53)))
    return out


def block_reply(request: DNSRecord) -> DNSRecord:
    reply = request.reply()
    q = request.q
    if q.qtype == QTYPE.A:
        reply.add_answer(RR(q.qname, QTYPE.A, ttl=BLOCK_TTL, rdata=A("0.0.0.0")))
    elif q.qtype == QTYPE.AAAA:
        reply.add_answer(RR(q.qname, QTYPE.AAAA, ttl=BLOCK_TTL, rdata=AAAA("::")))
    return reply


def cname_targets(answer: bytes) -> list[str]:
    """The names an upstream answer's CNAME records point to; none if it doesn't parse."""
    try:
        reply = DNSRecord.parse(answer)
    except Exception:
        return []
    return [str(rr.rdata) for rr in reply.rr if rr.rtype == QTYPE.CNAME]


def udp_limit(request: DNSRecord) -> int:
    """The largest UDP reply this client takes: its EDNS buffer size, at least 512 (RFC 6891)."""
    for rr in request.ar:
        if rr.rtype == QTYPE.OPT:
            return max(rr.rclass, UDP_MAX)
    return UDP_MAX


def truncated_reply(request: DNSRecord) -> DNSRecord:
    """An empty reply with TC set: the client asks again over TCP."""
    reply = request.reply()
    reply.header.tc = 1
    return reply


async def read_message(reader: asyncio.StreamReader) -> bytes:
    """One DNS message from a TCP stream: a 2-byte length, then the message."""
    size = int.from_bytes(await reader.readexactly(2), "big")
    return await reader.readexactly(size)


def frame(message: bytes) -> bytes:
    return len(message).to_bytes(2, "big") + message


def spoof_ip_from_env() -> str | None:
    """EDDIE_SPOOF_IP="192.168.4.1": the address devices reach Eddie's stub server on."""
    value = os.environ.get("EDDIE_SPOOF_IP", "").strip()
    if not value:
        return None
    try:
        A(value)
    except Exception:
        raise ValueError(f"expected an IPv4 address, got {value!r}")
    return value


def spoof_reply(request: DNSRecord, ip: str) -> DNSRecord:
    """Eddie's IP for A; an empty answer for AAAA and the rest, so clients fall back to IPv4."""
    reply = request.reply()
    if request.q.qtype == QTYPE.A:
        reply.add_answer(RR(request.q.qname, QTYPE.A, ttl=BLOCK_TTL, rdata=A(ip)))
    return reply


class _UpstreamReply(asyncio.DatagramProtocol):
    def __init__(self, txid: bytes):
        self.txid = txid
        self.answer = asyncio.get_running_loop().create_future()

    def datagram_received(self, data, addr):
        if data[:2] == self.txid and not self.answer.done():
            self.answer.set_result(data)

    def error_received(self, exc):
        if not self.answer.done():
            self.answer.set_exception(exc)


async def forward(packet: bytes, upstreams: list[tuple[str, int]], timeout: float = UPSTREAM_TIMEOUT) -> bytes | None:
    """Raw packet to each upstream in turn; first matching answer wins, None if all fail."""
    loop = asyncio.get_running_loop()
    for host, port in upstreams:
        transport = None
        try:
            transport, proto = await loop.create_datagram_endpoint(
                lambda: _UpstreamReply(packet[:2]), remote_addr=(host, port))
            transport.sendto(packet)
            return await asyncio.wait_for(proto.answer, timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("upstream %s:%s failed (%s)", host, port, type(exc).__name__)
        finally:
            if transport:
                transport.close()
    return None


async def _ask_tcp(packet: bytes, host: str, port: int) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(frame(packet))
        await writer.drain()
        return await read_message(reader)
    finally:
        writer.close()


async def forward_tcp(packet: bytes, upstreams: list[tuple[str, int]], timeout: float = UPSTREAM_TIMEOUT) -> bytes | None:
    """Like forward, over TCP, for answers too big for UDP."""
    for host, port in upstreams:
        try:
            answer = await asyncio.wait_for(_ask_tcp(packet, host, port), timeout)
            if answer[:2] == packet[:2]:
                return answer
            log.warning("upstream %s:%s answered another query over TCP", host, port)
        except (OSError, EOFError, asyncio.TimeoutError) as exc:
            log.warning("upstream %s:%s failed over TCP (%s)", host, port, type(exc).__name__)
    return None


async def resolve(packet: bytes, client_ip: str, upstreams: list[tuple[str, int]],
                  timeout: float = UPSTREAM_TIMEOUT, query_log: QueryLog | None = None,
                  feed: FeedPublisher | None = None, device_map: devices.DeviceMap | None = None,
                  tcp: bool = False) -> bytes | None:
    """The reply to send back, or None to drop a packet that isn't a DNS query.

    tcp: the query came over TCP, so it's forwarded over TCP and the reply may be any size.
    """
    try:
        request = DNSRecord.parse(packet)
    except Exception:
        return None
    if request.header.qr:
        return None
    if not request.questions:
        reply = request.reply()
        reply.header.rcode = RCODE.FORMERR
        return reply.pack()

    arrived = int(time.time() * 1000)
    domain = str(request.q.qname).rstrip(".").lower()
    device = device_map.key(client_ip) if device_map else client_ip  # MAC once ARP knows it
    verdict = decision.evaluate(domain, device, client_ip, arrived)
    qtype = QTYPE.get(request.q.qtype, str(request.q.qtype))
    mac = device if device != client_ip else None
    name = device_map.name(device) if device_map else None

    def record(verdict: decision.Decision) -> None:
        if query_log:
            query_log.log(domain, device, qtype, verdict.action, verdict.source, verdict.reason, ts=arrived)
        if feed:
            feed.publish(query_event(arrived, client_ip, domain, qtype, verdict.action, verdict.source,
                                     verdict.reason, mac=mac, name=name))

    if verdict.action == "spoof":
        record(verdict)
        return (spoof_reply(request, spoof["ip"]) if spoof["ip"] else block_reply(request)).pack()
    if verdict.action == "block":
        record(verdict)
        if verdict.source == "home_mode" and client_ip and not client_ip.startswith("127."):
            # nft is a subprocess (up to 2 s): run it beside the reply, never in front of it.
            asyncio.get_running_loop().run_in_executor(
                None, nft._run_nft, ["add", "element", "inet", "eddie", "homeblock", f"{{ {client_ip} }}"])
        return block_reply(request).pack()

    if request.q.qtype == QTYPE.AAAA and decision.matches(domain, decision.CAMERA_DOMAINS):
        return request.reply().pack()

    if offline_mode.on:
        answer = offline_mode.answer(request)
    else:
        answer = await (forward_tcp if tcp else forward)(packet, upstreams, timeout)
    if answer is None:
        record(verdict)
        reply = request.reply()
        reply.header.rcode = RCODE.SERVFAIL
        return reply.pack()
    if verdict.source not in ("rule", "allowlist") and (cloaked := decision.uncloak(domain, cname_targets(answer))):
        record(cloaked)
        return (spoof_reply(request, spoof["ip"]) if cloaked.action == "spoof" and spoof["ip"] else block_reply(request)).pack()
    record(verdict)
    if not offline_mode.on:
        offline_mode.put(request, answer)
        if geo["watch"]:  # a queue put; the lookup happens in geoip's worker thread
            geo["watch"].observe(arrived, mac or f"ip:{client_ip}", name or client_ip, domain, answer)
    if not tcp and len(answer) > udp_limit(request):
        return truncated_reply(request).pack()
    return answer


class DNSServer(asyncio.DatagramProtocol):
    def __init__(self, upstreams: list[tuple[str, int]], timeout: float = UPSTREAM_TIMEOUT,
                 query_log: QueryLog | None = None, feed: FeedPublisher | None = None,
                 device_map: devices.DeviceMap | None = None):
        self.upstreams = upstreams
        self.timeout = timeout
        self.query_log = query_log
        self.feed = feed
        self.device_map = device_map
        self.transport = None
        self.tasks: set[asyncio.Task] = set()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(self.tasks) >= MAX_IN_FLIGHT:
            return
        task = asyncio.ensure_future(self.answer(data, addr))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def answer(self, data: bytes, addr) -> None:
        reply = await resolve(data, addr[0], self.upstreams, self.timeout, self.query_log, self.feed, self.device_map)
        if reply and self.transport and not self.transport.is_closing():
            self.transport.sendto(reply, addr)


class TCPServer:
    """DNS over TCP on the same port. Queries on one connection are answered in turn."""

    def __init__(self, upstreams: list[tuple[str, int]], timeout: float = UPSTREAM_TIMEOUT,
                 query_log: QueryLog | None = None, feed: FeedPublisher | None = None,
                 device_map: devices.DeviceMap | None = None, idle: float = TCP_IDLE_TIMEOUT):
        self.upstreams = upstreams
        self.timeout = timeout
        self.query_log = query_log
        self.feed = feed
        self.device_map = device_map
        self.idle = idle
        self.open = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.open >= MAX_TCP_CONNECTIONS:
            writer.close()
            return
        self.open += 1
        client_ip = writer.get_extra_info("peername")[0]
        try:
            while True:
                packet = await asyncio.wait_for(read_message(reader), self.idle)
                reply = await resolve(packet, client_ip, self.upstreams, self.timeout, self.query_log,
                                      self.feed, self.device_map, tcp=True)
                if reply is None:
                    return  # not DNS: hang up
                writer.write(frame(reply))
                await asyncio.wait_for(writer.drain(), self.idle)
        except (OSError, EOFError, asyncio.TimeoutError):
            pass  # client left, went quiet or stopped reading
        finally:
            self.open -= 1
            writer.close()


async def serve(host: str, port: int, upstreams: list[tuple[str, int]],
                timeout: float = UPSTREAM_TIMEOUT, query_log: QueryLog | None = None,
                feed: FeedPublisher | None = None, device_map: devices.DeviceMap | None = None) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DNSServer(upstreams, timeout, query_log, feed, device_map), local_addr=(host, port))
    return transport


async def serve_tcp(host: str, port: int, upstreams: list[tuple[str, int]],
                    timeout: float = UPSTREAM_TIMEOUT, query_log: QueryLog | None = None,
                    feed: FeedPublisher | None = None, device_map: devices.DeviceMap | None = None,
                    idle: float = TCP_IDLE_TIMEOUT) -> asyncio.Server:
    server = TCPServer(upstreams, timeout, query_log, feed, device_map, idle)
    return await asyncio.start_server(server.handle, host, port)


class StoreWatcher:
    """Reloads rules, AI verdicts, offline, away, home and sensitivity modes and quarantine when they change.

    After a reload it records the rules, offline, away, quarantine, home and
    sensitivity revisions it loaded (rules.mark_loaded, offline.mark_loaded,
    away.mark_loaded, quarantine.mark_loaded, home.mark_loaded,
    sensitivity.mark_loaded): the API's proof that DNS is enforcing a change.

    poll runs in a worker thread that cancelling watch_store can't stop, so
    close takes the same lock: the connection is never closed mid-poll.
    """

    def __init__(self, path: str, engine: decision.Engine = decision.engine, mode: offline.Mode | None = None):
        self.conn = store.connect(path)
        self.engine = engine
        self.mode = mode or offline_mode
        self.seen = None
        self.lock = threading.Lock()
        self.closed = False

    def close(self) -> None:
        """Close the connection once any poll in progress has finished."""
        with self.lock:
            self.closed = True
            self.conn.close()

    def poll(self) -> bool:
        """Reload if rules or verdicts changed since the last poll. True if it did."""
        with self.lock:
            return not self.closed and self._poll()

    def _poll(self) -> bool:
        changed = self.conn.execute(
            "SELECT (SELECT count(*) || ':' || ifnull(max(id), 0) || ':' || ifnull(max(ts), 0) FROM rules),"
            " (SELECT count(*) || ':' || ifnull(max(ts), 0) FROM ai_verdicts),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'rules_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'offline_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'away_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'quarantine_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'home_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'sensitivity_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'spoof_rev')").fetchone()
        if changed == self.seen:
            return False
        self.engine.load_from_store(self.conn)
        if self.mode.on != offline.wanted(self.conn):
            self.mode.on = not self.mode.on
            log.info("offline mode %s", "on: not forwarding" if self.mode.on else "off: forwarding")
        # The revisions read before the load: a write that lands in between is
        # already loaded, and the next poll records it.
        rules.mark_loaded(self.conn, changed[2])
        offline.mark_loaded(self.conn, changed[3])
        away.mark_loaded(self.conn, changed[4])
        quarantine.mark_loaded(self.conn, changed[5])
        home.mark_loaded(self.conn, changed[6])
        sensitivity.mark_loaded(self.conn, changed[7])
        spoof_mode.mark_loaded(self.conn, changed[8])
        self.seen = changed
        return True


async def watch_store(watcher: StoreWatcher, every: float = RELOAD_EVERY) -> None:
    """Poll in a worker thread so DNS never waits on disk."""
    while True:
        try:
            await asyncio.to_thread(watcher.poll)
        except sqlite3.Error as exc:
            log.warning("reloading rules and verdicts failed (%s)", exc)
        await asyncio.sleep(every)


async def watch_devices(device_map: devices.DeviceMap, every: float = devices.TICK,
                        feed: FeedPublisher | None = None) -> None:
    """Read ARP when a new client IP appears (checked every TICK) or every READ_EVERY, off the event loop.

    New devices are sent to the API's new_device alert, also off the event loop.
    """
    while True:
        if device_map.due(time.monotonic()):
            try:
                arrived = await asyncio.to_thread(device_map.refresh, *device_map.take())
                if arrived and feed:
                    await asyncio.to_thread(feed.send_new_devices, arrived)
            except (sqlite3.Error, OSError) as exc:
                log.warning("recording devices failed (%s)", exc)
        await asyncio.sleep(every)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    host = os.environ.get("EDDIE_DNS_HOST", "0.0.0.0")
    port = int(os.environ.get("EDDIE_DNS_PORT", "53"))
    upstreams = upstreams_from_env()
    started = time.monotonic()
    try:
        allowlist, blocklist = lists.load_from_env()
    except OSError as exc:
        raise SystemExit(f"Can't load the allowlist or blocklists ({exc}). Blocking without the allowlist can break devices.")
    decision.engine.set_lists(allowlist, blocklist)
    decision.engine.block_doh = os.environ.get("EDDIE_BLOCK_DOH", "off") == "on"
    try:
        spoof["ip"] = spoof_ip_from_env()
    except ValueError as exc:
        raise SystemExit(f"EDDIE_SPOOF_IP refused: {exc}.")
    log.info("loaded %d blocklist and %d allowlist entries in %.2fs%s",
             len(blocklist), len(allowlist), time.monotonic() - started,
             ", blocking encrypted DNS resolver names" if decision.engine.block_doh else "")
    try:
        feed = feed_from_env()
    except ValueError as exc:
        raise SystemExit(f"EDDIE_API_URL refused: {exc}. Set EDDIE_FEED=off to run without the live feed.")
    query_log = query_log_from_env()
    device_map = devices.DeviceMap(query_log.path if query_log else None, leases=devices.leases_from_env())
    # Alerts go to the API like the feed, so without the feed there's nobody to tell.
    geo["watch"] = geoip.geo_from_env(feed.send_geo_alerts) if feed else None

    async def run():
        try:
            transport = await serve(host, port, upstreams, query_log=query_log, feed=feed, device_map=device_map)
            tcp = await serve_tcp(host, port, upstreams, query_log=query_log, feed=feed, device_map=device_map)
        except PermissionError:
            raise SystemExit(f"Port {port} needs root: run with sudo, or set EDDIE_DNS_PORT=8053 for development.")
        except OSError as exc:
            raise SystemExit(f"Can't listen on {host}:{port} ({exc}). On a Mac, turn off Internet Sharing, which holds port 53.")
        log.info("listening on %s:%s (UDP and TCP), upstream %s, query log %s, live feed %s, spoof %s, geo alerts %s",
                 host, port, ", ".join(f"{h}:{p}" for h, p in upstreams),
                 f"{query_log.path} ({query_log.retention_days:g} days)" if query_log else "off",
                 feed.url if feed else "off", spoof["ip"] or "off (spoof rules block)",
                 f"loading {geo['watch'].path}" if geo["watch"] else "off")
        watcher = asyncio.create_task(watch_store(StoreWatcher(query_log.path))) if query_log else None
        device_watcher = asyncio.create_task(watch_devices(device_map, feed=feed))
        stop = asyncio.Event()
        # docker stop / systemd send SIGTERM; stop cleanly so queued rows get flushed.
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        except (NotImplementedError, AttributeError):
            pass
        try:
            await stop.wait()
        finally:
            if watcher:
                watcher.cancel()
            device_watcher.cancel()
            transport.close()
            tcp.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if query_log:
            query_log.close()
        if geo["watch"]:
            geo["watch"].close()
        if feed:
            feed.close()
        device_map.close()


if __name__ == "__main__":
    main()
