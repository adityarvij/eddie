"""Eddie's SQLite store (eddie.db): schema plus the per-lookup query log.

The schema is the one in README "SQLite schema" and is created on first open.
QueryLog keeps disk off the DNS hot path: log() only drops a row on an
in-memory queue, and one writer thread batches rows into SQLite. If the
queue is full (disk stalled), rows are dropped and counted, never waited on.

Each queries row holds only domain, device, time (ms), qtype, action, source
and reason. No answers, upstream IPs, packet bytes or anything else.

The query log is household browsing history, so (see docs/PRIVACY.md):
- rows older than EDDIE_RETENTION_DAYS (default 7) are deleted on open and
  hourly, with secure_delete so their bytes are overwritten, not just unlinked;
- eddie.db and its -wal / -shm files are owner-only (0600);
- EDDIE_DB=off turns query logging off and nothing is written.
"""
import logging
import os
import queue
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, vendor TEXT, label TEXT, first_seen INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS queries (id INTEGER PRIMARY KEY, ts INTEGER, device_mac TEXT, domain TEXT, qtype TEXT, action TEXT, source TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, scope TEXT, device_mac TEXT, pattern TEXT, action TEXT, created_by TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS ai_verdicts (domain TEXT PRIMARY KEY, label TEXT, confidence REAL, reason TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS q_ts ON queries(ts);
CREATE INDEX IF NOT EXISTS q_dev ON queries(device_mac, ts);
"""

QUERY_COLUMNS = ("ts", "device_mac", "domain", "qtype", "action", "source", "reason")
MAX_PENDING = 10_000  # rows waiting for the writer before new ones are dropped
BATCH = 500  # rows per transaction at most
RETENTION_DAYS = 7  # long enough for the weekly report, no longer
PRUNE_EVERY = 3600.0  # seconds between retention passes

log = logging.getLogger("eddie.store")
_STOP = object()




def _owner_only(path: str) -> None:
    """Create the file 0600 if missing, and tighten it if it exists."""
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    os.chmod(path, 0o600)
    for suffix in ("-wal", "-shm"):  # SQLite creates these with the main file's mode
        if os.path.exists(path + suffix):
            os.chmod(path + suffix, 0o600)


def connect(path: str) -> sqlite3.Connection:
    """Open eddie.db (owner-only), creating the schema if it isn't there yet."""
    _owner_only(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA secure_delete=ON")  # pruned rows are zeroed on disk
    conn.execute("PRAGMA journal_mode=WAL")  # readers (API, agent) don't block the writer
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def prune(conn: sqlite3.Connection, retention_days: float, now_ms: int | None = None) -> int:
    """Delete queries rows and AI verdicts older than the retention window.

    Verdicts go too: the list of domains the household looked up is history
    even without times. A domain seen again is simply classified again.
    Returns rows deleted.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cutoff = now_ms - int(retention_days * 86_400_000)
    with conn:
        deleted = conn.execute("DELETE FROM queries WHERE ts < ?", (cutoff,)).rowcount
        deleted += conn.execute("DELETE FROM ai_verdicts WHERE ts < ?", (cutoff,)).rowcount
    if deleted:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # don't leave old pages in the WAL
    return deleted


class QueryLog:
    """Non-blocking writer for the queries table. Call close() to flush."""

    def __init__(self, path: str, max_pending: int = MAX_PENDING,
                 retention_days: float = RETENTION_DAYS, prune_every: float = PRUNE_EVERY):
        self.path = path
        self.pending: queue.Queue = queue.Queue(max_pending)
        self.dropped = 0
        self.retention_days = retention_days
        self.prune_every = prune_every
        conn = connect(path)  # schema errors surface here, not in the thread
        prune(conn, retention_days)
        conn.close()
        self.last_prune = time.monotonic()
        self.thread = threading.Thread(target=self._write_loop, name="eddie-store", daemon=True)
        self.thread.start()

    def log(self, domain: str, device_mac: str, qtype: str, action: str, source: str,
            reason: str = "", ts: int | None = None) -> None:
        row = (int(time.time() * 1000) if ts is None else ts,
               device_mac, domain, qtype, action, source, reason)
        try:
            self.pending.put_nowait(row)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("query log behind, %d rows dropped", self.dropped)

    def close(self, timeout: float = 5.0) -> None:
        self.pending.put(_STOP)
        self.thread.join(timeout)

    def _write_loop(self) -> None:
        conn = connect(self.path)
        sql = f"INSERT INTO queries ({', '.join(QUERY_COLUMNS)}) VALUES ({', '.join('?' * len(QUERY_COLUMNS))})"
        stopping = False
        while not stopping:
            rows = []
            try:  # wake at least once per prune interval, even with no lookups
                item = self.pending.get(timeout=self.prune_every)
            except queue.Empty:
                item = None
            while item is not None and item is not _STOP:
                rows.append(item)
                if len(rows) >= BATCH:
                    break
                try:
                    item = self.pending.get_nowait()
                except queue.Empty:
                    break
            stopping = item is _STOP
            try:
                if rows:
                    with conn:
                        conn.executemany(sql, rows)
                if time.monotonic() - self.last_prune >= self.prune_every:
                    self.last_prune = time.monotonic()
                    prune(conn, self.retention_days)
            except sqlite3.Error as exc:
                log.warning("query log write failed (%s), %d rows lost", exc, len(rows))
        conn.close()


def query_log_from_env() -> QueryLog | None:
    """EDDIE_DB (default eddie.db, or "off" for no query log), EDDIE_RETENTION_DAYS."""
    path = os.environ.get("EDDIE_DB", "eddie.db")
    if path == "off":
        return None
    return QueryLog(path, retention_days=float(os.environ.get("EDDIE_RETENTION_DAYS", RETENTION_DAYS)))
