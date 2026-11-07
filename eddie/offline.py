"""Offline mode (#26): Eddie answers lookups without asking any upstream resolver.

POST /offline-mode stores the wanted state in engine_state (offline, plus an
offline_rev counter bumped on every write). The DNS server's StoreWatcher
switches Mode.on and records the revision it loaded as offline_loaded, the
same handshake rules use (rules.py): the API reports a change as applied
only once offline_loaded reaches its revision.

While on, an allowed lookup is answered from Cache, the upstream answers
the DNS server kept in memory while online, or gets SERVFAIL. Blocked
lookups are answered as always. Nothing is forwarded.
"""
import sqlite3
import time

from dnslib import QTYPE, DNSRecord

MAX_ENTRIES = 4096  # cached answers; the oldest is dropped first
MAX_AGE = 86_400.0  # seconds a cached answer may still be served offline
STALE_TTL = 30  # TTL on an answer served past its own TTL (RFC 8767's suggestion)


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def wanted(conn: sqlite3.Connection) -> bool:
    """The offline state last written."""
    return bool(_state(conn, "offline"))


def revision(conn: sqlite3.Connection) -> int:
    return _state(conn, "offline_rev")


def loaded(conn: sqlite3.Connection) -> int:
    """The offline revision the DNS server last loaded."""
    return _state(conn, "offline_loaded")


def write(conn: sqlite3.Connection, on: bool) -> int:
    """Store the wanted state. Returns the revision to wait for."""
    with conn:
        rev = revision(conn) + 1
        conn.executemany("INSERT OR REPLACE INTO engine_state VALUES (?, ?)",
                         [("offline", int(on)), ("offline_rev", rev)])
    return rev


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('offline_loaded', ?)", (rev,))


def _key(request: DNSRecord) -> tuple:
    q = request.q
    return str(q.qname).lower(), q.qtype, q.qclass


class Mode:
    """Whether the DNS server forwards, and the answers it can give without forwarding."""

    def __init__(self, max_entries: int = MAX_ENTRIES, max_age: float = MAX_AGE):
        self.on = False
        self.max_entries = max_entries
        self.max_age = max_age
        self.cache: dict[tuple, tuple[bytes, float]] = {}

    def put(self, request: DNSRecord, answer: bytes, now: float | None = None) -> None:
        """Keep an upstream answer (not SERVFAIL). Raw bytes only: parsing waits until offline."""
        if len(answer) < 12 or answer[3] & 0x0F == 2:
            return
        key = _key(request)
        self.cache.pop(key, None)
        if len(self.cache) >= self.max_entries:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = (answer, time.monotonic() if now is None else now)

    def answer(self, request: DNSRecord, now: float | None = None) -> bytes | None:
        """The cached answer for this question, with the request's ID and TTLs aged; None if there's none."""
        hit = self.cache.get(_key(request))
        if hit is None:
            return None
        raw, stored = hit
        age = (time.monotonic() if now is None else now) - stored
        if age > self.max_age:
            del self.cache[_key(request)]
            return None
        reply = DNSRecord.parse(raw)
        reply.header.id = request.header.id
        reply.questions = request.questions  # keeps the client's letter case
        for rr in reply.rr + reply.auth + reply.ar:
            if rr.rtype != QTYPE.OPT:
                rr.ttl = max(rr.ttl - int(age), 0) or STALE_TTL
        return reply.pack()
