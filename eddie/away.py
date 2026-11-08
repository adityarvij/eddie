"""Away mode (#109): cut telemetry from devices whose traffic shows someone's home.

POST /mode {"away": true} ranks devices by occupancy leak (rank), stores the
covered ones in away_devices and the wanted state in engine_state (away,
away_until, plus an away_rev counter bumped on every write). The DNS
server's StoreWatcher loads them into the decision engine and records the
revision it loaded as away_loaded, the same handshake rules and offline
mode use: the API reports away mode as applied only once away_loaded
reaches its revision.

Occupancy leak score, an interim stand-in until #34: the number of hours in
the last week in which a device either looked up media (labels.HUMAN) or
made at least BURST times its usual hourly lookups. Both mean someone was
using it, so its traffic going quiet says the house is empty. Devices that
score 0 aren't covered.

Security devices are never covered: a Ring, Arlo or Wyze vendor, a label or
hostname naming a camera, doorbell or alarm, or any lookup of a camera,
doorbell or alarm domain this week. They're stored with security = 1 only
so the dashboard and voice can say they stay up.

While away is on and before away_until (ms, 0 for no end time), a covered
device's lookups of labelled telemetry, AI "tracker" verdicts and beacons
are blocked (decision.py). Rules and the allowlist still come first.
Turning away off deletes every away_devices row.
"""
import re
import sqlite3
import statistics
from collections import defaultdict

from . import devices
from .decision import PLUMBING_SQL
from .labels import DOMAINS, HUMAN

WINDOW_MS = 7 * 86_400_000  # the query log keeps a week
HOUR_MS = 3_600_000
BURST = 3  # an hour with this many times the device's median hourly lookups...
MIN_BURST = 30  # ...and at least this many is someone using it
MAX_HOURS = 30 * 24  # the latest end time accepted
SECURITY_VENDORS = {"Ring", "Arlo", "Wyze"}
SECURITY_CATEGORIES = {"camera", "doorbell", "alarm"}
SECURITY_WORDS = re.compile(r"\b(?:cam|camera|cams|cameras|doorbell|alarm|security|ring|arlo|wyze|simplisafe)\b")


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def wanted(conn: sqlite3.Connection) -> bool:
    """The away state last written, whether or not its end time has passed."""
    return bool(_state(conn, "away"))


def until(conn: sqlite3.Connection) -> int:
    """The end time last written in ms, 0 for none."""
    return _state(conn, "away_until")


def revision(conn: sqlite3.Connection) -> int:
    return _state(conn, "away_rev")


def loaded(conn: sqlite3.Connection) -> int:
    """The away revision the DNS server last loaded."""
    return _state(conn, "away_loaded")


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('away_loaded', ?)", (rev,))


def active(conn: sqlite3.Connection, now_ms: int) -> bool:
    """On and not past its end time."""
    end = until(conn)
    return wanted(conn) and (not end or now_ms < end)


def write(conn: sqlite3.Connection, on: bool, end_ms: int | None = None,
          covered: list[dict] = (), exempt: list[dict] = ()) -> int:
    """Store the wanted state and the devices it covers. Returns the revision to wait for."""
    with conn:
        conn.execute("DELETE FROM away_devices")
        if on:
            conn.executemany("INSERT INTO away_devices VALUES (?, ?, ?)",
                             [(d["key"], d["score"], 0) for d in covered] + [(d["key"], 0, 1) for d in exempt])
        rev = revision(conn) + 1
        conn.executemany("INSERT OR REPLACE INTO engine_state VALUES (?, ?)",
                         [("away", int(on)), ("away_until", (end_ms or 0) if on else 0), ("away_rev", rev)])
    return rev


def covered_keys(conn: sqlite3.Connection) -> frozenset[str]:
    """The device keys the engine blocks telemetry for while away is on."""
    return frozenset(r[0] for r in conn.execute("SELECT mac FROM away_devices WHERE security = 0"))


def stored(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """(covered devices by score, exempt security devices) as stored, with display names."""
    covered, exempt = [], []
    for key, score, security in conn.execute(
            "SELECT mac, score, security FROM away_devices ORDER BY score DESC, mac"):
        row = {"key": key, "name": devices.name_of(conn, key)}
        if security:
            exempt.append(row)
        else:
            covered.append({**row, "score": score})
    return covered, exempt


def category(domain: str) -> str:
    """labels.lookup's category alone: the hand-written table, no ownership lookup."""
    parts = domain.split(".")
    return next((DOMAINS[s][1] for s in (".".join(parts[i:]) for i in range(len(parts))) if s in DOMAINS), "unknown")


def is_security(label: str | None, hostname: str | None, vendor: str | None, categories: set[str]) -> bool:
    words = " ".join(re.sub(r"[^a-z0-9]+", " ", (n or "").lower()) for n in (label, hostname))
    return (vendor in SECURITY_VENDORS or bool(SECURITY_WORDS.search(words))
            or bool(categories & SECURITY_CATEGORIES))


def rank(conn: sqlite3.Connection, now_ms: int) -> tuple[list[dict], list[dict]]:
    """(covered, exempt) from the last week of the query log.

    covered: {key, name, score} for devices scoring above 0, highest first.
    exempt: {key, name} for security devices that looked anything up.
    """
    hours: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    media: dict[str, set[int]] = defaultdict(set)
    categories: dict[str, set[str]] = defaultdict(set)
    for key, domain, hour, count in conn.execute(
            "SELECT device_mac, domain, ts / ? AS hour, COUNT(*) FROM queries"
            f" WHERE ts >= ? AND NOT {PLUMBING_SQL} GROUP BY device_mac, domain, hour",
            (HOUR_MS, now_ms - WINDOW_MS)):
        hours[key][hour] += count
        cat = category(domain)
        categories[key].add(cat)
        if cat in HUMAN:
            media[key].add(hour)
    known = {r[0]: r[1:] for r in conn.execute("SELECT mac, label, hostname, vendor FROM devices")}
    covered, exempt = [], []
    for key, counts in hours.items():
        label, hostname, vendor = known.get(key, (None, None, None))
        row = {"key": key, "name": devices.display_name(key, label, hostname, vendor)}
        if is_security(label, hostname, vendor, categories[key]):
            exempt.append(row)
            continue
        floor = max(MIN_BURST, BURST * statistics.median(counts.values()))
        score = len(media[key] | {h for h, n in counts.items() if n >= floor})
        if score:
            covered.append({**row, "score": score})
    covered.sort(key=lambda d: (-d["score"], d["name"]))
    exempt.sort(key=lambda d: d["name"])
    return covered, exempt
