"""User rules in eddie.db: the store side of the rules API (#12).

Every write (add or remove) also bumps rules_rev in the engine_state table in
the same transaction. The DNS server's StoreWatcher reloads the rules into the
decision engine and then records the revision it loaded as rules_loaded. A
writer that sees rules_loaded >= its own revision knows the running DNS
engine is enforcing the change; that, not the stored row, is the
acknowledgment the API hands back (docs/EVENTS.md "Rules").
"""
import re
import sqlite3
import time
from typing import Literal

from pydantic import BaseModel, field_validator

# A domain of two or more labels, optionally "*." for it and its subdomains.
# A wildcard over a bare TLD ("*.com") is refused.
PATTERN = re.compile(r"(\*\.)?[a-z0-9_-]+(\.[a-z0-9_-]+)+")
# A MAC address, or the client IP the DNS server uses until #14 maps devices.
DEVICE = re.compile(r"[0-9a-z:._-]{1,64}")
CREATED_BY = re.compile(r"[a-z_]{1,32}")
COLUMNS = ("id", "scope", "device_mac", "pattern", "action", "created_by", "ts")


class RuleIn(BaseModel):
    pattern: str
    action: Literal["allow", "block"]
    device_mac: str | None = None  # None for a rule that covers every device
    created_by: str = "api"

    @field_validator("pattern")
    @classmethod
    def check_pattern(cls, v: str) -> str:
        v = v.strip().lower().rstrip(".")
        if len(v) > 253 or not PATTERN.fullmatch(v):
            raise ValueError("expected a domain like a.com, or *.a.com for it and its subdomains")
        return v

    @field_validator("device_mac")
    @classmethod
    def check_device(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not DEVICE.fullmatch(v):
            raise ValueError("expected a MAC address or client IP")
        return v

    @field_validator("created_by")
    @classmethod
    def check_created_by(cls, v: str) -> str:
        if not CREATED_BY.fullmatch(v):
            raise ValueError("expected a short lowercase name such as voice or dashboard")
        return v


def _row(row) -> dict:
    return dict(zip(COLUMNS, row))


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def _bump(conn: sqlite3.Connection) -> int:
    rev = _state(conn, "rules_rev") + 1
    conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('rules_rev', ?)", (rev,))
    return rev


def revision(conn: sqlite3.Connection) -> int:
    """The latest rules revision written."""
    return _state(conn, "rules_rev")


def loaded(conn: sqlite3.Connection) -> int:
    """The rules revision the DNS engine last loaded."""
    return _state(conn, "rules_loaded")


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('rules_loaded', ?)", (rev,))


def all_rules(conn: sqlite3.Connection) -> list[dict]:
    """Every rule, newest first."""
    return [_row(r) for r in conn.execute(f"SELECT {', '.join(COLUMNS)} FROM rules ORDER BY id DESC")]


def add(conn: sqlite3.Connection, rule: RuleIn, now_ms: int | None = None) -> tuple[dict, int]:
    """Store the rule. Returns (stored row, revision to wait for)."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    scope = "device" if rule.device_mac else "global"
    with conn:
        cur = conn.execute(
            "INSERT INTO rules (scope, device_mac, pattern, action, created_by, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (scope, rule.device_mac, rule.pattern, rule.action, rule.created_by, ts))
        rev = _bump(conn)
    return {"id": cur.lastrowid, "scope": scope, "device_mac": rule.device_mac, "pattern": rule.pattern,
            "action": rule.action, "created_by": rule.created_by, "ts": ts}, rev


def remove(conn: sqlite3.Connection, rule_id: int) -> tuple[dict | None, int]:
    """Delete the rule. Returns (deleted row or None if there was none, revision)."""
    with conn:
        row = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM rules WHERE id = ?", (rule_id,)).fetchone()
        if row is None:
            return None, revision(conn)
        conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        rev = _bump(conn)
    return _row(row), rev
