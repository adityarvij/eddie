"""Sends each DNS decision to the live feed: POST /ingest on the local API.

The DNS server only drops the event on an in-memory queue; one sender thread
batches events and posts them, so DNS never waits on the API. If the API is
down or slow, events are dropped and counted, never retried: the feed is a
live view, and the query log (store.py) is the history.

Two kinds of lookup are left out of the feed (they're still answered and in
the query log): network plumbing (decision.is_plumbing: reverse lookups,
service discovery), and repeats of the same device, domain and decision
within REPEAT_MS, such as a browser asking for A and AAAA at once.

New device alerts (#98) go to POST /devices/new, sent by send_new_devices
from the device watcher's thread: rare, so not queued, and dropped if the API
doesn't answer (the device stays pending in eddie.db for the dashboard).
Geo alerts (#102) go to POST /geo/alerts the same way, from geoip's worker.

Events use the query format in docs/EVENTS.md. Until devices.py (#14) maps an
IP to a MAC and label, device_mac is "ip:<client IP>" and device is the IP.

Privacy (docs/PRIVACY.md): EDDIE_API_URL (default http://127.0.0.1:8000) must
be localhost or a private IP literal, redirects aren't followed and proxy
settings are ignored, so lookups can't leave the house through the feed.
Events are never written to the log. EDDIE_FEED=off sends nothing.
"""
import json
import logging
import os
import queue
import threading
import urllib.request

from .classifier import _OPENER, auth_headers, check_local
from .decision import is_plumbing

DEFAULT_URL = "http://127.0.0.1:8000"
MAX_PENDING = 1_000  # events waiting for the sender before new ones are dropped
BATCH = 100  # events per POST at most
TIMEOUT = 1.0  # seconds per POST
REPEAT_MS = 2_000  # same device, domain and decision again within this is one feed line
MAX_RECENT = 10_000  # (device, domain, decision) keys remembered for REPEAT_MS

log = logging.getLogger("eddie.feed")
_STOP = object()


def query_event(ts: int, client_ip: str, domain: str, qtype: str,
                action: str, source: str, reason: str, mac: str | None = None, name: str | None = None) -> dict:
    """device_mac is the MAC devices.py found, else "ip:<client IP>"; device is its name, else the IP."""
    return {"type": "query", "ts": ts, "device_mac": mac or f"ip:{client_ip}", "device": name or client_ip,
            "domain": domain, "qtype": qtype, "action": action, "source": source, "reason": reason}


class FeedPublisher:
    """Non-blocking sender to /ingest. Call close() to send what's queued."""

    def __init__(self, url: str = DEFAULT_URL, max_pending: int = MAX_PENDING, timeout: float = TIMEOUT):
        self.url = url.rstrip("/")
        check_local(self.url)
        self.pending: queue.Queue = queue.Queue(max_pending)
        self.timeout = timeout
        self.dropped = 0
        self.failing = False
        self.last_sent: dict[tuple, int] = {}  # (device_mac, domain, action) -> ts
        self.thread = threading.Thread(target=self._send_loop, name="eddie-feed", daemon=True)
        self.thread.start()

    def publish(self, event: dict) -> None:
        """Queue a query event, unless it's plumbing or a repeat. Never blocks."""
        if is_plumbing(event["domain"]):
            return
        key = (event["device_mac"], event["domain"], event["action"])
        last = self.last_sent.get(key)
        if last is not None and 0 <= event["ts"] - last < REPEAT_MS:
            return
        if len(self.last_sent) >= MAX_RECENT:
            self.last_sent = {k: t for k, t in self.last_sent.items() if event["ts"] - t < REPEAT_MS}
            if len(self.last_sent) >= MAX_RECENT:
                self.last_sent.clear()
        self.last_sent[key] = event["ts"]
        try:
            self.pending.put_nowait(event)
        except queue.Full:
            self._drop(1, "feed behind")

    def close(self, timeout: float = 2.0) -> None:
        self.pending.put(_STOP)
        self.thread.join(timeout)

    def _drop(self, n: int, why: str) -> None:
        before = self.dropped
        self.dropped += n
        if before == 0 or before // 1000 != self.dropped // 1000:
            log.warning("%s, %d live-feed events dropped", why, self.dropped)

    def _send_loop(self) -> None:
        stopping = False
        while not stopping:
            item = self.pending.get()
            events = []
            while item is not _STOP:
                events.append(item)
                if len(events) >= BATCH:
                    break
                try:
                    item = self.pending.get_nowait()
                except queue.Empty:
                    break
            stopping = item is _STOP
            if events:
                self._send(events)

    def send_new_devices(self, alerts: list[dict]) -> bool:
        """POST new_device alerts to the API. Blocking (one timeout): call from a worker thread."""
        return self._send(alerts, "/devices/new")

    def send_geo_alerts(self, alerts: list[dict]) -> bool:
        """POST geo_alert events (#102) to the API. Blocking: geoip's worker thread calls it."""
        return self._send(alerts, "/geo/alerts")

    def _send(self, events: list[dict], path: str = "/ingest") -> bool:
        req = urllib.request.Request(self.url + path, data=json.dumps(events).encode(),
                                     headers={"Content-Type": "application/json", **auth_headers()})
        try:
            with _OPENER.open(req, timeout=self.timeout) as response:
                response.read()
        except OSError as exc:  # URLError, HTTPError and timeouts are all OSError
            if not self.failing:
                log.warning("live feed unreachable at %s (%s)", self.url, type(exc).__name__)
            self.failing = True
            self.dropped += len(events)
            return False
        if self.failing:
            log.info("live feed reachable again at %s", self.url)
            self.failing = False
        return True


def feed_from_env() -> FeedPublisher | None:
    """EDDIE_FEED=off sends nothing; EDDIE_API_URL picks the API (local only)."""
    if os.environ.get("EDDIE_FEED", "on") == "off":
        return None
    return FeedPublisher(os.environ.get("EDDIE_API_URL", DEFAULT_URL))
