"""Eddie's DNS server: answers every lookup from devices pointed at Eddie.

Allowed lookups are forwarded to an upstream resolver (1.1.1.1, then 9.9.9.9)
and the answer is passed back untouched. Blocked lookups get a dead end:
0.0.0.0 for A, :: for AAAA, an empty answer for anything else, TTL 60.
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
worker thread, and records the devices seen in eddie.db (#14).
Offline mode (#26, offline.py) is loaded the same way: while it's on, nothing
is forwarded and allowed lookups get the answer kept from the last time they
were forwarded, or SERVFAIL.
Each decision is also sent to the live feed (POST /ingest on the local API)
by feed.FeedPublisher, off the reply path; EDDIE_FEED=off turns that off.

Run (port 53):            sudo .venv/bin/python -m eddie.dns_server
Development (no sudo):    EDDIE_DNS_PORT=8053 uv run python -m eddie.dns_server
                          dig @127.0.0.1 -p 8053 google.com
"""
import asyncio
import logging
import os
import signal
import sqlite3
import time

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSRecord

from . import decision, devices, lists, offline, rules
from . import store
from .feed import FeedPublisher, feed_from_env, query_event
from .store import QueryLog, query_log_from_env

BLOCK_TTL = 60
UPSTREAM_TIMEOUT = 2.0  # seconds per upstream before trying the next one
RELOAD_EVERY = 0.2  # seconds between checks of eddie.db for new rules or verdicts
MAX_IN_FLIGHT = 512  # lookups being answered at once; extra packets are dropped

log = logging.getLogger("eddie.dns")
offline_mode = offline.Mode()


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


async def resolve(packet: bytes, client_ip: str, upstreams: list[tuple[str, int]],
                  timeout: float = UPSTREAM_TIMEOUT, query_log: QueryLog | None = None,
                  feed: FeedPublisher | None = None, device_map: devices.DeviceMap | None = None) -> bytes | None:
    """The reply to send back, or None to drop a packet that isn't a DNS query."""
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
    if query_log:
        query_log.log(domain, device, qtype, verdict.action, verdict.source, verdict.reason, ts=arrived)
    if feed:
        feed.publish(query_event(arrived, client_ip, domain, qtype, verdict.action, verdict.source, verdict.reason,
                                 mac=device if device != client_ip else None,
                                 name=device_map.name(device) if device_map else None))
    if verdict.action == "block":
        return block_reply(request).pack()

    answer = offline_mode.answer(request) if offline_mode.on else await forward(packet, upstreams, timeout)
    if answer is None:
        reply = request.reply()
        reply.header.rcode = RCODE.SERVFAIL
        return reply.pack()
    if not offline_mode.on:
        offline_mode.put(request, answer)
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


async def serve(host: str, port: int, upstreams: list[tuple[str, int]],
                timeout: float = UPSTREAM_TIMEOUT, query_log: QueryLog | None = None,
                feed: FeedPublisher | None = None, device_map: devices.DeviceMap | None = None) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: DNSServer(upstreams, timeout, query_log, feed, device_map), local_addr=(host, port))
    return transport


class StoreWatcher:
    """Reloads rules, AI verdicts and offline mode when they change.

    After a reload it records the rules and offline revisions it loaded
    (rules.mark_loaded, offline.mark_loaded): the API's proof that DNS is
    enforcing a change.
    """

    def __init__(self, path: str, engine: decision.Engine = decision.engine, mode: offline.Mode | None = None):
        self.conn = store.connect(path)
        self.engine = engine
        self.mode = mode or offline_mode
        self.seen = None

    def poll(self) -> bool:
        """Reload if rules or verdicts changed since the last poll. True if it did."""
        changed = self.conn.execute(
            "SELECT (SELECT count(*) || ':' || ifnull(max(id), 0) || ':' || ifnull(max(ts), 0) FROM rules),"
            " (SELECT count(*) || ':' || ifnull(max(ts), 0) FROM ai_verdicts),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'rules_rev'),"
            " (SELECT ifnull(max(value), 0) FROM engine_state WHERE key = 'offline_rev')").fetchone()
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


async def watch_devices(device_map: devices.DeviceMap, every: float = devices.TICK) -> None:
    """Read ARP when a new client IP appears (checked every TICK) or every READ_EVERY, off the event loop."""
    while True:
        if device_map.due(time.monotonic()):
            try:
                await asyncio.to_thread(device_map.refresh, *device_map.take())
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
    log.info("loaded %d blocklist and %d allowlist entries in %.2fs",
             len(blocklist), len(allowlist), time.monotonic() - started)
    try:
        feed = feed_from_env()
    except ValueError as exc:
        raise SystemExit(f"EDDIE_API_URL refused: {exc}. Set EDDIE_FEED=off to run without the live feed.")
    query_log = query_log_from_env()
    device_map = devices.DeviceMap(query_log.path if query_log else None, leases=devices.leases_from_env())

    async def run():
        try:
            transport = await serve(host, port, upstreams, query_log=query_log, feed=feed, device_map=device_map)
        except PermissionError:
            raise SystemExit(f"Port {port} needs root: run with sudo, or set EDDIE_DNS_PORT=8053 for development.")
        except OSError as exc:
            raise SystemExit(f"Can't listen on {host}:{port} ({exc}). On a Mac, turn off Internet Sharing, which holds port 53.")
        log.info("listening on %s:%s, upstream %s, query log %s, live feed %s",
                 host, port, ", ".join(f"{h}:{p}" for h, p in upstreams),
                 f"{query_log.path} ({query_log.retention_days:g} days)" if query_log else "off",
                 feed.url if feed else "off")
        watcher = asyncio.create_task(watch_store(StoreWatcher(query_log.path))) if query_log else None
        device_watcher = asyncio.create_task(watch_devices(device_map))
        stop = asyncio.Event()
        # docker stop / systemd send SIGTERM; stop cleanly so queued rows get flushed.
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        try:
            await stop.wait()
        finally:
            if watcher:
                watcher.cancel()
            device_watcher.cancel()
            transport.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    finally:
        if query_log:
            query_log.close()
        if feed:
            feed.close()
        device_map.close()


if __name__ == "__main__":
    main()
