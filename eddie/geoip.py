"""Geo alerts (#102): "Your doorbell started talking to a server in another country."

The DNS server hands each forwarded answer to GeoWatch.observe, which only
puts it on a bounded in-memory queue (dropped when full), so DNS never waits.
A worker thread pulls the A and AAAA addresses out of the answer, looks each
public one up in an offline country database and keeps, per device, the set
of countries its answers pointed to.

Like device baselines (baselines.py), a device first learns: its countries are
recorded silently until it has had MIN_ANSWERS answers *and* LEARN_MS has
passed since its first. After that, an answer in a country it has never used
gives one geo_alert (docs/EVENTS.md) naming the device, domain and country, and
the country joins its set. A device that raises more than UNSTABLE_NEW new
countries within UNSTABLE_MS (a phone or laptop roaming the web) stops raising
them. Everything is in memory and a restart learns again.

IP geolocation is approximate: CDNs and cloud providers answer from many
countries, and a database row says where an address is registered or routed,
not where the data ends up. Every alert says so.

The database is DB-IP's IP to Country Lite CSV (start,end,country rows, sorted
and non-overlapping; gzip or plain), pinned by SHA-256 in eddie/data/geoip.json
and fetched only by scripts/provision_geoip.py. It is hashed and loaded in the
worker thread when the DNS server starts; a missing file or a wrong hash turns
geo alerts off with one log line. Nothing here touches the network except the
alert to the local API. EDDIE_GEOIP=off turns it off; EDDIE_GEOIP=<path> loads
another file, which must still match the pinned hash.
"""
import csv
import gzip
import hashlib
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
from array import array
from bisect import bisect_right
from pathlib import Path

from dnslib import QTYPE, DNSRecord

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("data") / "geoip.json"
COUNTRIES = Path(__file__).with_name("data") / "countries.json"
MIN_ANSWERS = 50
LEARN_MS = 60 * 60_000
UNSTABLE_NEW = 5  # new countries within UNSTABLE_MS before a device stops raising them
UNSTABLE_MS = 24 * 3_600_000
MAX_DEVICES = 1_024  # devices remembered; the least recently seen is forgotten
MAX_PENDING = 1_000  # answers waiting for the worker before new ones are dropped
NO_COUNTRY = 0  # index for gaps and DB-IP's "ZZ" (reserved or unknown)
CAVEAT = ("IP location is approximate: CDNs and cloud providers serve from many countries, "
          "so this is where the server's address is registered, not proof of where the data goes.")
_LOW = (1 << 64) - 1

log = logging.getLogger("eddie.geoip")
_STOP = object()


class GeoIPError(Exception):
    pass


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text())


def country_names(path: Path = COUNTRIES) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


class _Starts:
    """128-bit range starts in two arrays (a list of Python ints would cost ~4x the memory)."""

    def __init__(self):
        self.hi = array("Q")
        self.lo = array("Q")

    def append(self, n: int) -> None:
        self.hi.append(n >> 64)
        self.lo.append(n & _LOW)

    def __len__(self) -> int:
        return len(self.lo)

    def __getitem__(self, i: int) -> int:
        return self.hi[i] << 64 | self.lo[i]


class _Table:
    """Sorted range starts, each with a country index; a gap is a start with NO_COUNTRY."""

    def __init__(self, starts):
        self.starts = starts
        self.country = array("H")
        self.end = -1  # last address covered so far

    def add(self, start: int, end: int, index: int) -> None:
        if start <= self.end or end < start:
            raise GeoIPError("rows must be sorted, non-overlapping ranges")
        if start > self.end + 1:
            self.starts.append(self.end + 1)
            self.country.append(NO_COUNTRY)
        self.starts.append(start)
        self.country.append(index)
        self.end = end

    def find(self, n: int) -> int:
        i = bisect_right(self.starts, n) - 1
        return NO_COUNTRY if i < 0 or n > self.end else self.country[i]


class CountryDB:
    """IP -> ISO country code from a start,end,country CSV."""

    def __init__(self):
        self.codes = [""]  # index 0 is NO_COUNTRY
        self.v4 = _Table(array("I"))
        self.v6 = _Table(_Starts())

    @classmethod
    def load(cls, path: str | Path) -> "CountryDB":
        db = cls()
        index: dict[str, int] = {}
        opener = gzip.open if str(path).endswith(".gz") else open
        with opener(path, "rt", newline="", encoding="ascii") as f:
            for line, row in enumerate(csv.reader(f), 1):
                try:
                    start, end, code = row
                    six = ":" in start
                    family = socket.AF_INET6 if six else socket.AF_INET
                    a = int.from_bytes(socket.inet_pton(family, start), "big")
                    b = int.from_bytes(socket.inet_pton(family, end), "big")
                except (ValueError, OSError):
                    raise GeoIPError(f"line {line}: expected start,end,country")
                code = code.strip().upper()
                if code in ("", "ZZ"):
                    i = NO_COUNTRY
                elif (i := index.get(code)) is None:
                    i = index[code] = len(db.codes)
                    db.codes.append(code)
                try:
                    (db.v6 if six else db.v4).add(a, b, i)
                except GeoIPError as exc:
                    raise GeoIPError(f"line {line}: {exc}")
        return db

    def country(self, ip: str) -> str | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        table = self.v6 if addr.version == 6 else self.v4
        return self.codes[table.find(int(addr))] or None


def load_verified(path: str | Path, expected_sha256: str) -> CountryDB:
    """The database at path, only if its SHA-256 is the pinned one."""
    if not Path(path).is_file():
        raise GeoIPError(f"{path} is missing; run python scripts/provision_geoip.py while online")
    actual = sha256(path)
    if actual != expected_sha256:
        raise GeoIPError(f"{path} has SHA-256 {actual}, expected the pinned {expected_sha256}")
    return CountryDB.load(path)


def answer_ips(answer: bytes) -> list[str]:
    """The public A and AAAA addresses in a DNS answer (CNAMEs followed by the upstream)."""
    try:
        reply = DNSRecord.parse(answer)
    except Exception:
        return []
    out = []
    for rr in reply.rr:
        if rr.rtype in (QTYPE.A, QTYPE.AAAA):
            ip = str(rr.rdata)
            if ipaddress.ip_address(ip).is_global and ip not in out:
                out.append(ip)
    return out


class Destinations:
    """Per device: the countries its answers pointed to, and when to start alerting."""

    def __init__(self, names: dict[str, str] | None = None, min_answers: int = MIN_ANSWERS,
                 learn_ms: int = LEARN_MS, unstable_new: int = UNSTABLE_NEW,
                 unstable_ms: int = UNSTABLE_MS, max_devices: int = MAX_DEVICES):
        self.names = names if names is not None else country_names()
        self.min_answers = min_answers
        self.learn_ms = learn_ms
        self.unstable_new = unstable_new
        self.unstable_ms = unstable_ms
        self.max_devices = max_devices
        self.devices: dict[str, dict] = {}  # device_mac -> state, least recently seen first

    def observe(self, ts: int, device_mac: str, device: str, domain: str,
                found: list[tuple[str, str]]) -> list[dict]:
        """Record one answer's (ip, country) pairs; the geo_alert events it causes."""
        d = self.devices.pop(device_mac, None)
        if d is None:
            if len(self.devices) >= self.max_devices:
                del self.devices[next(iter(self.devices))]
            d = {"started": ts, "answers": 0, "countries": {}, "new": [], "unstable": False}
        self.devices[device_mac] = d
        d["answers"] += 1
        learning = d["answers"] <= self.min_answers or ts - d["started"] < self.learn_ms
        alerts = []
        for ip, code in found:
            if code in d["countries"]:
                continue
            before = sorted(d["countries"])
            d["countries"][code] = ts
            if learning or d["unstable"]:
                continue
            d["new"] = [t for t in d["new"] if ts - t < self.unstable_ms] + [ts]
            if len(d["new"]) > self.unstable_new:
                d["unstable"] = True
                log.info("%s reached %d new countries in a day; no more geo alerts for it", device, len(d["new"]))
                continue
            alerts.append(self.alert(ts, device_mac, device, domain, ip, code, before))
        return alerts

    def alert(self, ts: int, device_mac: str, device: str, domain: str, ip: str,
              code: str, before: list[str]) -> dict:
        name = self.names.get(code, code)
        usual = ", ".join(self.names.get(c, c) for c in before[:3]) + (" and others" if len(before) > 3 else "")
        reason = (f"{device} started talking to a server in {name} ({domain}, {ip})."
                  + (f" Until now its servers were in {usual}." if usual else "") + f" {CAVEAT}")
        return {"type": "geo_alert", "ts": ts, "device_mac": device_mac, "device": device, "domain": domain,
                "ip": ip, "country": code, "country_name": name, "previous": before,
                "approximate": True, "reason": reason}


class GeoWatch:
    """Non-blocking: observe() queues an answer; a worker thread loads the database, looks up and alerts.

    send(alerts) delivers geo_alert events (feed.FeedPublisher.send_geo_alerts);
    it blocks the worker, never DNS.
    """

    def __init__(self, path: str | Path, expected_sha256: str, send, destinations: Destinations | None = None,
                 max_pending: int = MAX_PENDING):
        self.path = path
        self.expected = expected_sha256
        self.send = send
        self.destinations = destinations or Destinations()
        self.pending: queue.Queue = queue.Queue(max_pending)
        self.db: CountryDB | None = None
        self.off = False  # the database failed to load: queue nothing more
        self.dropped = 0
        self.ready = threading.Event()  # set once loading finished, either way
        self.thread = threading.Thread(target=self._run, name="eddie-geoip", daemon=True)
        self.thread.start()

    def observe(self, ts: int, device_mac: str, device: str, domain: str, answer: bytes) -> None:
        """Queue a forwarded answer. Never blocks."""
        if self.off:
            return
        try:
            self.pending.put_nowait((ts, device_mac, device, domain, answer))
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                log.warning("geo lookups behind, %d answers skipped", self.dropped)

    def close(self, timeout: float = 2.0) -> None:
        self.pending.put(_STOP)
        self.thread.join(timeout)

    def _run(self) -> None:
        try:
            self.db = load_verified(self.path, self.expected)
            log.info("geo alerts on: %d IPv4 and %d IPv6 ranges from %s",
                     len(self.db.v4.country), len(self.db.v6.country), self.path)
        except (GeoIPError, OSError, UnicodeDecodeError) as exc:
            self.off = True
            log.warning("geo alerts off: %s", exc)
        finally:
            self.ready.set()
        while (item := self.pending.get()) is not _STOP:
            if self.db is None:
                continue  # drain until close
            try:
                alerts = self.check(*item)
                if alerts:
                    self.send(alerts)
            except Exception:
                log.exception("geo lookup failed")

    def check(self, ts: int, device_mac: str, device: str, domain: str, answer: bytes) -> list[dict]:
        found = [(ip, code) for ip in answer_ips(answer) if (code := self.db.country(ip))]
        return self.destinations.observe(ts, device_mac, device, domain, found) if found else []


def geo_from_env(send) -> GeoWatch | None:
    """EDDIE_GEOIP=off: no geo alerts. Otherwise the pinned file (or EDDIE_GEOIP=<path>), hash-checked."""
    value = os.environ.get("EDDIE_GEOIP", "").strip()
    if value == "off":
        return None
    pinned = manifest()
    path = Path(value) if value else ROOT / pinned["path"]
    return GeoWatch(path, pinned["sha256"], send)
