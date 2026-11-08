"""Sensitivity modes (#110): block third-party trackers around a whole category.

"Hey Eddie, protect anything health related" turns on the health mode. Each
mode covers a group of domain labels (labels.DOMAINS):

    health   health, pharmacy and telehealth
    kids     children's apps and sites
    money    banking, payments, credit and shopping
    beliefs  news, religion and politics

DNS never sees which page asked for a tracker, so the rule set works per
device and in time: a lookup of one of the group's first-party domains (the
pharmacy site itself) opens a WINDOW_MS window for that device, and inside it
the device's lookups of a known third-party tracker are blocked with source
sensitivity. The tracker set is every domain labelled telemetry, ads,
analytics, tracking or id-sync plus every data broker and ad exchange
(labels.BROKERS); AI "tracker" verdicts are blocked in the window too
(decision.py). First-party domains are never in the tracker set, so the site
keeps working. Rules and the allowlist still come first.

The wanted modes live in engine_state as sensitive_<mode> (1 while on) with
a sensitivity_rev counter bumped on every write. The DNS server's
StoreWatcher loads them into the decision engine and records the revision it
loaded as sensitivity_loaded, the same handshake rules, away and home mode
use: the API reports a change as applied only once sensitivity_loaded
reaches its revision. Windows are held in memory only and never stored.
"""
import sqlite3

from .labels import BROKERS, DOMAINS, TRACKING

MODES = {
    "health": frozenset({"health"}),
    "kids": frozenset({"kids"}),
    "money": frozenset({"finance", "shopping"}),
    "beliefs": frozenset({"news", "religion", "politics"}),
}
WINDOW_MS = 5 * 60_000  # how long after a first-party lookup the device's trackers stay blocked


def first_party(mode: str) -> frozenset[str]:
    """The labelled domains the mode protects; a lookup of one opens the window."""
    return frozenset(d for d, (_, category) in DOMAINS.items() if category in MODES[mode])


def trackers() -> frozenset[str]:
    """Third-party tracker domains blocked inside a window: labelled tracking plus brokers."""
    return frozenset(d for d, (_, category) in DOMAINS.items() if category in TRACKING) | frozenset(BROKERS)


def rule_set(mode: str) -> tuple[frozenset[str], frozenset[str]]:
    """(first-party domains, tracker domains) for one mode. A first-party domain is never a tracker."""
    sites = first_party(mode)
    return sites, trackers() - sites


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def wanted(conn: sqlite3.Connection) -> list[str]:
    """The modes last turned on, in MODES order."""
    return [m for m in MODES if _state(conn, f"sensitive_{m}")]


def revision(conn: sqlite3.Connection) -> int:
    return _state(conn, "sensitivity_rev")


def loaded(conn: sqlite3.Connection) -> int:
    """The sensitivity revision the DNS server last loaded."""
    return _state(conn, "sensitivity_loaded")


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('sensitivity_loaded', ?)", (rev,))


def write(conn: sqlite3.Connection, mode: str, on: bool) -> int:
    """Turn one mode on or off. Returns the revision to wait for."""
    if mode not in MODES:
        raise ValueError(f"unknown sensitivity mode {mode!r}")
    with conn:
        rev = revision(conn) + 1
        conn.executemany("INSERT OR REPLACE INTO engine_state VALUES (?, ?)",
                         [(f"sensitive_{mode}", int(on)), ("sensitivity_rev", rev)])
    return rev


def describe(mode: str) -> dict:
    """{mode, sites, trackers, examples}: rule set sizes and up to three site owners, for the API and voice."""
    sites, found = rule_set(mode)
    examples = list(dict.fromkeys(DOMAINS[d][0] for d in sorted(sites)))[:3]
    return {"mode": mode, "sites": len(sites), "trackers": len(found), "examples": examples}
