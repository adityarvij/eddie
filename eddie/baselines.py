"""Device baselines: learn each device's normal domains, then point out new ones.

A device's baseline starts learning at the first feed event the API sees from
it. Every registrable domain (labels.registrable_domain: cdn1.foo.com and
cdn7.foo.com are both foo.com) it looks up is added. Learning ends once the
device has made at least MIN_LOOKUPS lookups *and* LEARN_MS has passed since
the first one: a busy laptop passes 200 lookups in minutes and a quiet plug
may take days, so neither alone is a fair sample.

After that the baseline is active. A registrable domain the device has never
looked up gives one new_domain event and joins the baseline, so it never
fires again for that device. It says "first time", never "malicious": a
learning window always misses domains a device uses only now and then.

Phones and laptops visit new sites all day, so a baseline means little there.
If an active device produces more than UNSTABLE_NEW new domains within
UNSTABLE_MS, it becomes unstable: domains are still recorded but no more
new_domain events are sent. Resetting the baseline starts learning again.

Counts are of feed events (docs/EVENTS.md), so plumbing and 2 s repeats are
already left out. Everything lives in eddie.db (baselines, baseline_domains)
and is pruned with the query log: a domain not looked up for
EDDIE_RETENTION_DAYS is forgotten and counts as new again (store.prune).
Runs in the API on feed events, never on the DNS hot path.
"""
import os
import sqlite3

from .labels import registrable_domain

MIN_LOOKUPS = 200
LEARN_MS = 60 * 60_000
UNSTABLE_NEW = 20  # new domains within UNSTABLE_MS before a device stops raising them
UNSTABLE_MS = 24 * 3_600_000
TOUCH_MS = 3_600_000  # a known domain's last_seen is rewritten at most this often
RECENT_NEW = 20  # new domains listed in state()


def key_of(device_mac: str) -> str:
    """The feed's "ip:<client IP>" is the bare IP everywhere else (devices, rules)."""
    return device_mac.removeprefix("ip:")


def settings_from_env() -> tuple[int, int]:
    """EDDIE_BASELINE_LOOKUPS (default 200) and EDDIE_BASELINE_MINUTES (default 60)."""
    return (int(os.environ.get("EDDIE_BASELINE_LOOKUPS", MIN_LOOKUPS)),
            int(float(os.environ.get("EDDIE_BASELINE_MINUTES", LEARN_MS / 60_000)) * 60_000))


def observe(conn: sqlite3.Connection, ev: dict, min_lookups: int = MIN_LOOKUPS,
            learn_ms: int = LEARN_MS) -> list[dict]:
    """Record one query event; the new_domain / baseline_state events it causes.

    The caller commits (one transaction per /ingest batch).
    """
    key, ts = key_of(ev["device_mac"]), ev["ts"]
    domain = registrable_domain(ev["domain"])
    row = conn.execute("SELECT state, started_at, lookups FROM baselines WHERE device_mac = ?", (key,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO baselines (device_mac, state, started_at, lookups, active_at, last_seen) "
                     "VALUES (?, 'learning', ?, 0, NULL, ?)", (key, ts, ts))
        row = ("learning", ts, 0)
    state, started_at, lookups = row
    known = conn.execute("SELECT last_seen FROM baseline_domains WHERE device_mac = ? AND domain = ?",
                         (key, domain)).fetchone()
    if known is not None:
        if ts - known[0] >= TOUCH_MS:
            conn.execute("UPDATE baseline_domains SET last_seen = ? WHERE device_mac = ? AND domain = ?",
                         (ts, key, domain))
    else:
        conn.execute("INSERT INTO baseline_domains (device_mac, domain, first_seen, last_seen, learned) "
                     "VALUES (?, ?, ?, ?, ?)", (key, domain, ts, ts, state == "learning"))

    out = []
    if state == "learning":
        lookups += 1
        if lookups >= min_lookups and ts - started_at >= learn_ms:
            state = "active"
            conn.execute("UPDATE baselines SET state = 'active', active_at = ? WHERE device_mac = ?", (ts, key))
            out.append(state_event(conn, key, ev, "active"))
        conn.execute("UPDATE baselines SET lookups = ?, last_seen = max(last_seen, ?) WHERE device_mac = ?",
                     (lookups, ts, key))
    elif known is None and state == "active":
        recent = conn.execute("SELECT count(*) FROM baseline_domains WHERE device_mac = ? AND learned = 0 "
                              "AND first_seen > ?", (key, ts - UNSTABLE_MS)).fetchone()[0]
        if recent > UNSTABLE_NEW:
            conn.execute("UPDATE baselines SET state = 'unstable' WHERE device_mac = ?", (key,))
            out.append(state_event(conn, key, ev, "unstable"))
        else:
            out.append({"type": "new_domain", "ts": ts, "device_mac": key, "device": ev["device"],
                        "domain": domain, "query": ev["domain"], "action": ev["action"]})
    if state != "learning":
        conn.execute("UPDATE baselines SET last_seen = ? WHERE device_mac = ? AND last_seen <= ?",
                     (ts, key, ts - TOUCH_MS))
    return out


def state_event(conn: sqlite3.Connection, key: str, ev: dict, state: str) -> dict:
    domains = conn.execute("SELECT count(*) FROM baseline_domains WHERE device_mac = ?", (key,)).fetchone()[0]
    return {"type": "baseline_state", "ts": ev["ts"], "device_mac": key, "device": ev["device"],
            "state": state, "domains": domains}


def state(conn: sqlite3.Connection, key: str, now_ms: int, min_lookups: int = MIN_LOOKUPS,
          learn_ms: int = LEARN_MS) -> dict | None:
    """A device's baseline for the API, or None if it has none."""
    row = conn.execute("SELECT state, started_at, lookups, active_at, last_seen FROM baselines "
                       "WHERE device_mac = ?", (key,)).fetchone()
    if row is None:
        return None
    st, started_at, lookups, active_at, last_seen = row
    learned, added = conn.execute("SELECT count(*) FILTER (WHERE learned), count(*) FILTER (WHERE NOT learned) "
                                  "FROM baseline_domains WHERE device_mac = ?", (key,)).fetchone()
    new = conn.execute("SELECT domain, first_seen FROM baseline_domains WHERE device_mac = ? AND learned = 0 "
                       "ORDER BY first_seen DESC LIMIT ?", (key, RECENT_NEW)).fetchall()
    learning = st == "learning"
    return {"device_mac": key, "state": st, "started_at": started_at, "active_at": active_at,
            "last_seen": last_seen, "lookups": lookups, "domains": learned + added,
            "learned_domains": learned, "new_domain_count": added,
            "lookups_left": max(0, min_lookups - lookups) if learning else 0,
            "ms_left": max(0, started_at + learn_ms - now_ms) if learning else 0,
            "min_lookups": min_lookups, "learn_ms": learn_ms,
            "new_domains": [{"domain": d, "first_seen": t} for d, t in new]}


def all_keys(conn: sqlite3.Connection) -> list[str]:
    return [k for (k,) in conn.execute("SELECT device_mac FROM baselines ORDER BY started_at")]


def reset(conn: sqlite3.Connection, key: str) -> int | None:
    """Forget a device's baseline; its next lookup starts learning. Domains deleted, or None if it had none."""
    with conn:
        if conn.execute("DELETE FROM baselines WHERE device_mac = ?", (key,)).rowcount == 0:
            return None
        return conn.execute("DELETE FROM baseline_domains WHERE device_mac = ?", (key,)).rowcount
