"""Device identification (#14): client IP -> MAC -> friendly name.

The DNS server only sees a client IP. DeviceMap turns it into a MAC from the
kernel's ARP table (`ip neigh` on Linux, `arp -an` on macOS), read in a worker
thread; the DNS hot path only reads a dict. A device's key (queries.device_mac,
rules.device_mac) is its MAC once known, otherwise its client IP.

A device's name is, first match wins: the user's label, the hostname from a
DHCP leases file (dnsmasq format, router mode only), "<vendor> device" from
the MAC's OUI prefix, then the key. Vendors come from the bundled table
data/oui_vendors.json (scripts/build_oui.py); nothing is fetched at runtime.
Hostnames are never looked up with reverse DNS, which would send the
household's private IPs to an upstream resolver.

Each read also rebuilds the name of every mapped device, so the live feed can
show "Fire Stick" without reading the database during a lookup; a new label
reaches the feed at the next read (at most READ_EVERY).

Only devices that look something up through Eddie are written to the devices
table, never every ARP neighbour. Rows without a label are pruned with the
query log (store.prune); labels are user settings and are kept.
"""
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import store

READ_EVERY = 30.0  # seconds between ARP reads when no new device has appeared
TICK = 1.0  # seconds between checks for new client IPs
MAX_TRACKED = 4096  # client IPs held between reads; more are ignored until the next read
LEASES = "/var/lib/misc/dnsmasq.leases"
MAX_LABEL = 40
RECENT_BLOCKS = 3  # blocked domains listed per device by GET /devices
COLUMNS = ("mac", "ip", "hostname", "vendor", "label", "first_seen", "last_seen")

log = logging.getLogger("eddie.devices")
_VENDORS = {prefix: name for name, prefixes in
            json.loads((Path(__file__).parent / "data" / "oui_vendors.json").read_text()).items()
            for prefix in prefixes}
_IP_NEIGH = re.compile(r"^(\S+)\s.*\blladdr\s+([0-9a-fA-F:]+)")
_ARP = re.compile(r"\(([0-9a-fA-F.:]+)\) at ([0-9a-fA-F:]+)")
_HOSTNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")


def normalize_mac(mac: str) -> str | None:
    """aa:bb:cc:dd:ee:ff, padding macOS's dropped zeros ("0:1c:..."); None if not a usable MAC."""
    parts = re.split(r"[:-]", mac.strip().lower())
    if len(parts) != 6 or not all(re.fullmatch(r"[0-9a-f]{1,2}", p) for p in parts):
        return None
    mac = ":".join(p.zfill(2) for p in parts)
    if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") or int(parts[0], 16) & 1:  # multicast
        return None
    return mac


def is_random(mac: str) -> bool:
    """Locally administered: phones' private Wi-Fi addresses. The OUI means nothing."""
    return bool(int(mac[:2], 16) & 2)


def vendor(mac: str) -> str | None:
    if is_random(mac):
        return None
    return _VENDORS.get(mac.replace(":", "")[:6])


def parse_ip_neigh(text: str) -> dict[str, str]:
    """`ip neigh show` -> {ip: mac}. FAILED and INCOMPLETE entries have no lladdr and are skipped."""
    out = {}
    for line in text.splitlines():
        m = _IP_NEIGH.match(line.strip())
        if m and (mac := normalize_mac(m[2])):
            out[m[1]] = mac
    return out


def parse_arp(text: str) -> dict[str, str]:
    """`arp -an` (macOS) -> {ip: mac}. "(incomplete)" entries are skipped."""
    out = {}
    for m in _ARP.finditer(text):
        if mac := normalize_mac(m[2]):
            out[m[1]] = mac
    return out


def read_neighbours(timeout: float = 2.0) -> dict[str, str]:
    """The kernel's ARP table, {} if it can't be read."""
    cmd, parse = (["arp", "-an"], parse_arp) if sys.platform == "darwin" else (["ip", "neigh", "show"], parse_ip_neigh)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("can't read the ARP table with %s (%s)", cmd[0], type(exc).__name__)
        return {}
    return parse(result.stdout)


def parse_leases(text: str) -> dict[str, str]:
    """dnsmasq leases ("expiry mac ip hostname client-id") -> {mac: hostname}. "*" means none."""
    out = {}
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 4 and (mac := normalize_mac(cols[1])) and _HOSTNAME.fullmatch(cols[3]):
            out[mac] = cols[3]
    return out


def read_leases(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    try:
        return parse_leases(Path(path).read_text(errors="replace"))
    except OSError:
        return {}


def leases_from_env() -> str | None:
    """EDDIE_DHCP_LEASES (default dnsmasq's file when it exists; "off" for none)."""
    path = os.environ.get("EDDIE_DHCP_LEASES")
    if path == "off":
        return None
    return path or (LEASES if os.path.exists(LEASES) else None)


def display_name(key: str, label: str | None, hostname: str | None, vendor_name: str | None) -> str:
    return label or hostname or (f"{vendor_name} device" if vendor_name else key)


def name_of(conn: sqlite3.Connection, key: str) -> str:
    """A device key's display name, or the key itself if Eddie hasn't recorded that device."""
    row = conn.execute("SELECT label, hostname, vendor FROM devices WHERE mac = ?", (key,)).fetchone()
    return display_name(key, *row) if row else key


def clean_label(label: str | None) -> str | None:
    """A label as stored: trimmed, or None to clear it. ValueError if unusable."""
    label = (label or "").strip()
    if not label:
        return None
    if len(label) > MAX_LABEL or not label.isprintable():
        raise ValueError(f"expected a printable name of at most {MAX_LABEL} characters")
    return label


def upsert(conn: sqlite3.Connection, seen: dict[str, str], macs: dict[str, str],
           hostnames: dict[str, str], now_ms: int) -> int:
    """Record devices (client IP -> MAC) that looked something up. Never touches labels."""
    rows = [(mac, ip, hostnames.get(mac), vendor(mac), now_ms, now_ms)
            for ip, mac in macs.items() if ip in seen]
    with conn:
        conn.executemany(
            "INSERT INTO devices (mac, ip, hostname, vendor, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(mac) DO UPDATE SET ip = excluded.ip, vendor = excluded.vendor,"
            " hostname = coalesce(excluded.hostname, devices.hostname), last_seen = excluded.last_seen", rows)
    return len(rows)


class DeviceMap:
    """IP -> MAC for the DNS server. key() runs on the hot path; refresh() in a worker thread."""

    def __init__(self, db_path: str | None = None, read=read_neighbours, leases: str | None = None):
        self.db_path = db_path
        self.read = read
        self.leases = leases
        self.macs: dict[str, str] = {}  # replaced whole by refresh, never mutated
        self.names: dict[str, str] = {}  # device key -> name, when there's more to say than the key
        self.seen: set[str] = set()  # client IPs since the last refresh
        self.new: set[str] = set()  # client IPs that should trigger a read: no MAC yet, or not recorded
        self.tried: set[str] = set()  # client IPs a read already failed to resolve
        self.recorded: set[str] = set()  # MACs written to the devices table at least once
        self.last_read = float("-inf")
        self.conn = None

    def key(self, ip: str) -> str:
        """The device key for a client IP: its MAC if known, else the IP. No I/O."""
        if len(self.seen) < MAX_TRACKED:
            self.seen.add(ip)
        mac = self.macs.get(ip)
        if (ip not in self.tried if mac is None else mac not in self.recorded) and len(self.new) < MAX_TRACKED:
            self.new.add(ip)
        return mac or ip

    def name(self, key: str) -> str | None:
        """The device's label, hostname or "<vendor> device"; None if only its key is known. No I/O."""
        return self.names.get(key)

    def due(self, now: float) -> bool:
        return bool(self.new) or now - self.last_read >= READ_EVERY

    def take(self) -> tuple[set[str], set[str]]:
        """Hand the IPs seen so far to a refresh. Call on the event loop thread."""
        seen, new = self.seen, self.new
        self.seen, self.new = set(), set()
        return seen, new

    def refresh(self, seen: set[str], new: set[str], now_ms: int | None = None) -> None:
        """Read ARP and the leases file, then record the devices seen. Blocking: run in a thread."""
        self.last_read = time.monotonic()
        macs = self.read()
        self.macs = macs
        self.tried = (self.tried | {ip for ip in new if ip not in macs}) if len(self.tried) < MAX_TRACKED else set()
        hostnames = read_leases(self.leases)
        labels = {}
        if not self.db_path:
            self.recorded = set(macs.values())  # nothing to record
        else:
            if self.conn is None:
                self.conn = store.connect(self.db_path)
            if seen:
                now_ms = int(time.time() * 1000) if now_ms is None else now_ms
                upsert(self.conn, seen, macs, hostnames, now_ms)
                written = {macs[ip] for ip in seen if ip in macs}
                self.recorded = (self.recorded | written) if len(self.recorded) < MAX_TRACKED else written
            labels = dict(self.conn.execute("SELECT mac, label FROM devices WHERE label IS NOT NULL"))
        names = {mac: hostnames.get(mac) or (f"{v} device" if (v := vendor(mac)) else None) for mac in macs.values()}
        self.names = {k: n for k, n in names.items() if n} | labels

    def close(self) -> None:
        if self.conn:
            self.conn.close()


def _row(row) -> dict:
    return dict(zip(COLUMNS, row))


def all_devices(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """Every known device plus any key in the query log since since_ms, with lookup counts.

    blocked counts lookups Eddie blocked (trackers, ads, rules); flagged ones
    were forwarded while unknown. trackers counts distinct blocked domains, and
    recent_blocks holds the last RECENT_BLOCKS of them, newest first.
    Most blocked first.
    """
    devices = {r[0]: _row(r) for r in conn.execute(f"SELECT {', '.join(COLUMNS)} FROM devices")}
    counts = {key: (n, blocked or 0, flagged or 0, last) for key, n, blocked, flagged, last in conn.execute(
        "SELECT device_mac, COUNT(*), SUM(action = 'block'), SUM(action = 'flag'), MAX(ts)"
        " FROM queries WHERE ts >= ? GROUP BY device_mac", (since_ms,))}
    blocks: dict[str, list[tuple[int, str]]] = {}
    for key, domain, last in conn.execute(
            "SELECT device_mac, domain, MAX(ts) FROM queries WHERE ts >= ? AND action = 'block'"
            " GROUP BY device_mac, domain", (since_ms,)):
        blocks.setdefault(key, []).append((last, domain))
    out = []
    for key in devices.keys() | counts.keys():
        d = devices.get(key) or dict.fromkeys(COLUMNS) | {"mac": key}
        n, blocked, flagged, last = counts.get(key, (0, 0, 0, None))
        domains = sorted(blocks.get(key, ()), reverse=True)
        out.append({**d, "name": display_name(key, d["label"], d["hostname"], d["vendor"]),
                    "last_seen": max(filter(None, (d["last_seen"], last)), default=None),
                    "queries": n, "blocked": blocked, "flagged": flagged, "trackers": len(domains),
                    "recent_blocks": [{"domain": dom, "ts": ts} for ts, dom in domains[:RECENT_BLOCKS]]})
    return sorted(out, key=lambda d: (-d["blocked"], -d["queries"], d["mac"]))


def set_label(conn: sqlite3.Connection, key: str, label: str | None) -> dict | None:
    """Label (or with None, unlabel) a known device. None if Eddie has never seen it."""
    with conn:
        known = (conn.execute("SELECT 1 FROM devices WHERE mac = ?", (key,)).fetchone()
                 or conn.execute("SELECT 1 FROM queries WHERE device_mac = ? LIMIT 1", (key,)).fetchone())
        if not known:
            return None
        # A device known only from the query log is keyed by its client IP: no MAC yet.
        ip = None if normalize_mac(key) == key else key
        conn.execute("INSERT INTO devices (mac, ip, label) VALUES (?, ?, ?)"
                     " ON CONFLICT(mac) DO UPDATE SET label = excluded.label", (key, ip, label))
        row = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM devices WHERE mac = ?", (key,)).fetchone()
    return _row(row)
