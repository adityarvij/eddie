"""New device alert and quarantine (#98): "a device I've never seen joined your network".

device_status in eddie.db holds every MAC Eddie has decided about:
- approved: a household device; quarantine never touches it.
- pending: first seen after the household was known; nobody has decided yet.
- denied: the user said no.

The first time devices are recorded (an empty device_status), every device
already in the devices table and every one seen in that read is approved
without an alert: they're the household Eddie was installed into. After
that, devices.DeviceMap.refresh calls record_new for each MAC that looked
something up, and a MAC with no row becomes pending; the DNS server sends a
new_device event for it (docs/EVENTS.md).

Quarantine is opt-in (engine_state quarantine, off by default). While on, a
pending device gets block answers for everything except the essentials
allowlist. A denied device gets them whether quarantine is on or not. Like
offline mode (offline.py), every write bumps quarantine_rev, the DNS server's
StoreWatcher reloads the blocked set into the decision engine and records
quarantine_loaded, and the API reports a change as applied only once
quarantine_loaded reaches its revision.

Quarantine is DNS-only: a device that uses its own resolver or hard-coded IP
addresses still gets out. Router mode (#22/#23) is what stops that.
"""
import sqlite3
import time
from typing import Iterable

APPROVED, PENDING, DENIED = "approved", "pending", "denied"
COLUMNS = ("mac", "status", "first_seen", "decided_at")


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def _bump(conn: sqlite3.Connection) -> int:
    rev = revision(conn) + 1
    conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('quarantine_rev', ?)", (rev,))
    return rev


def wanted(conn: sqlite3.Connection) -> bool:
    """Whether quarantine is on (last written)."""
    return bool(_state(conn, "quarantine"))


def revision(conn: sqlite3.Connection) -> int:
    return _state(conn, "quarantine_rev")


def loaded(conn: sqlite3.Connection) -> int:
    """The quarantine revision the DNS server last loaded."""
    return _state(conn, "quarantine_loaded")


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('quarantine_loaded', ?)", (rev,))


def write(conn: sqlite3.Connection, on: bool) -> int:
    """Turn quarantine on or off. Returns the revision to wait for."""
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('quarantine', ?)", (int(on),))
        return _bump(conn)


def record_new(conn: sqlite3.Connection, macs: Iterable[str], now_ms: int | None = None) -> list[str]:
    """Record MACs that looked something up. Returns the ones seen for the first time (now pending).

    The first call on an empty device_status approves every known device instead
    and returns nothing.
    """
    macs = sorted(set(macs))
    if not macs:
        return []
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    with conn:
        if conn.execute("SELECT 1 FROM device_status LIMIT 1").fetchone() is None:
            known = {m for (m,) in conn.execute("SELECT mac FROM devices")} | set(macs)
            conn.executemany("INSERT OR IGNORE INTO device_status VALUES (?, ?, ?, ?)",
                             [(m, APPROVED, now_ms, now_ms) for m in sorted(known)])
            return []
        have = {m for (m,) in conn.execute(
            f"SELECT mac FROM device_status WHERE mac IN ({', '.join('?' * len(macs))})", macs)}
        new = [m for m in macs if m not in have]
        if new:
            conn.executemany("INSERT INTO device_status VALUES (?, ?, ?, NULL)", [(m, PENDING, now_ms) for m in new])
            _bump(conn)
    return new


def decide(conn: sqlite3.Connection, mac: str, status: str, now_ms: int | None = None) -> tuple[dict | None, int]:
    """Approve or deny a device Eddie has recorded. Returns (row or None if unknown, revision to wait for)."""
    if status not in (APPROVED, DENIED):
        raise ValueError("expected approved or denied")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    with conn:
        known = (conn.execute("SELECT 1 FROM device_status WHERE mac = ?", (mac,)).fetchone()
                 or conn.execute("SELECT 1 FROM devices WHERE mac = ?", (mac,)).fetchone())
        if not known:
            return None, revision(conn)
        conn.execute("INSERT INTO device_status VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(mac) DO UPDATE SET status = excluded.status, decided_at = excluded.decided_at",
                     (mac, status, now_ms, now_ms))
        rev = _bump(conn)
        row = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM device_status WHERE mac = ?", (mac,)).fetchone()
    return dict(zip(COLUMNS, row)), rev


def statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """{mac: approved | pending | denied} for every MAC with a row."""
    return dict(conn.execute("SELECT mac, status FROM device_status"))


def blocked(conn: sqlite3.Connection) -> frozenset[str]:
    """MACs the DNS engine should answer with blocks: denied ones, and pending ones while quarantine is on."""
    held = (DENIED, PENDING) if wanted(conn) else (DENIED,)
    return frozenset(m for (m,) in conn.execute(
        f"SELECT mac FROM device_status WHERE status IN ({', '.join('?' * len(held))})", held))
