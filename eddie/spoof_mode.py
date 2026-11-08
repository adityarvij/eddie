"""Acknowledged global spoof mode.

The API stores the wanted state and DNS records the revision it loaded.  The
decision engine uses it only for tracker-style blocking decisions; explicit
rules, quarantine, Home, Away and sensitivity protections keep their meaning.
"""
import sqlite3


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def wanted(conn: sqlite3.Connection) -> bool:
    return bool(_state(conn, "spoof_mode"))


def revision(conn: sqlite3.Connection) -> int:
    return _state(conn, "spoof_rev")


def loaded(conn: sqlite3.Connection) -> int:
    return _state(conn, "spoof_loaded")


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('spoof_loaded', ?)", (rev,))


def write(conn: sqlite3.Connection, on: bool) -> int:
    with conn:
        rev = revision(conn) + 1
        conn.executemany("INSERT OR REPLACE INTO engine_state VALUES (?, ?)",
                         [("spoof_mode", int(on)), ("spoof_rev", rev)])
    return rev
