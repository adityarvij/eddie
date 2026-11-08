"""Home mode (#20): block indoor cameras' cloud uploads while a household phone is home.

A device marked as a household phone (PUT /devices/{key} {"phone": true}) is
home when Eddie saw it in the last PRESENT_MS: its devices row's last_seen
(ARP or DHCP) or its latest lookup in the query log. Phones join Wi-Fi with a
private address that stays the same for each network, so that per-network
MAC is the key to mark, not the hardware one.

The wanted state lives in engine_state: home (on or off), home_manual (1
while a dashboard or voice change overrides presence) and a home_rev counter
bumped on every write. The DNS server's StoreWatcher loads it into the
decision engine and records the revision it loaded as home_loaded, the same
handshake rules, offline and away mode use: the API reports home mode as
applied only once home_loaded reaches its revision.

Presence (the API's watch_presence) turns home mode on when the first marked
phone is home and off when the last one leaves. A manual change wins and
holds until POST /mode {"home_auto": true} hands control back to presence.

While on, the decision engine blocks CAMERA_DOMAINS and their subdomains with
source home_mode. Rules and the allowlist still come first.
"""
import sqlite3

PRESENT_MS = 10 * 60_000  # a phone seen this recently is home
# Indoor cameras' upload domains. Ring's exact upload hosts aren't confirmed, so the whole of ring.com.
CAMERA_DOMAINS = frozenset({"ring.com"})


def _state(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else 0


def wanted(conn: sqlite3.Connection) -> bool:
    """The home mode state last written."""
    return bool(_state(conn, "home"))


def manual(conn: sqlite3.Connection) -> bool:
    """True while a dashboard or voice change overrides presence."""
    return bool(_state(conn, "home_manual"))


def revision(conn: sqlite3.Connection) -> int:
    return _state(conn, "home_rev")


def loaded(conn: sqlite3.Connection) -> int:
    """The home revision the DNS server last loaded."""
    return _state(conn, "home_loaded")


def mark_loaded(conn: sqlite3.Connection, rev: int) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO engine_state VALUES ('home_loaded', ?)", (rev,))


def write(conn: sqlite3.Connection, on: bool, is_manual: bool) -> int:
    """Store the wanted state and whether it overrides presence. Returns the revision to wait for."""
    with conn:
        rev = revision(conn) + 1
        conn.executemany("INSERT OR REPLACE INTO engine_state VALUES (?, ?)",
                         [("home", int(on)), ("home_manual", int(is_manual)), ("home_rev", rev)])
    return rev


def phones_home(conn: sqlite3.Connection, now_ms: int) -> list[str]:
    """Keys of the marked household phones seen in the last PRESENT_MS."""
    return [r[0] for r in conn.execute(
        "SELECT mac FROM devices d WHERE phone = 1 AND max(ifnull(last_seen, 0),"
        " ifnull((SELECT MAX(ts) FROM queries WHERE device_mac = d.mac), 0)) >= ? ORDER BY mac",
        (now_ms - PRESENT_MS,))]
