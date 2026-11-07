"""Traffic feed: takes events in on /ingest and pushes them out on /ws/events.

The fake generator (scripts/fake_traffic.py) and the DNS server (eddie/feed.py)
both POST here; Codex's frontend listens on the WebSocket.

/rules writes user rules to eddie.db (EDDIE_DB, the DNS server's database) and
waits for the DNS server to load them before answering; see docs/EVENTS.md.
/devices lists the devices the DNS server recorded there, with lookup counts
from its query log, and stores user labels (devices.py).
/offline-mode turns the DNS server's upstream forwarding off or on through
eddie.db and waits for DNS to load it the same way (offline.py).

Writes (POST, PUT, DELETE) from anywhere but loopback need EDDIE_API_TOKEN as
a bearer token (#96). Browsers may only write or open /ws/events from this
machine's own pages; see allowed_origin.

Run: uv run uvicorn eddie.api:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hmac
import ipaddress
import itertools
import json
import os
import sqlite3
import time
from collections import deque
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from . import beacons, demo_week, devices, inferences, insights, labels, offline, rules, store
from .events import Action, AIVerdictEvent, ModeUpdate, OfflineUpdate, QueryEvent

# In memory for now; SQLite (store.py) replaces this once the DNS side lands.
recent: deque[dict] = deque(maxlen=20000)
# The feed keeps no more history than eddie.db does (#129).
FEED_RETENTION_MS = int(float(os.environ.get("EDDIE_RETENTION_DAYS", store.RETENTION_DAYS)) * 86_400_000)
totals = {"seen": 0, "blocked": 0}
state = {"home_mode": False}
clients: set[WebSocket] = set()
ai_verdicts: dict[str, dict] = {}
timing = {"beacons": beacons.Beacons(), "idle": beacons.Idle()}  # beacon_s and idle on each event (#24)
ids = itertools.count(1)
db: dict[str, sqlite3.Connection | None] = {"conn": None}
APPLY_TIMEOUT = 2.0  # seconds to wait for DNS to load a rule or offline change (it polls every 0.2 s)
APPLY_POLL = 0.02

app = FastAPI(title="Eddie traffic")
WRITES = {"POST", "PUT", "PATCH", "DELETE"}


def is_loopback(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def authorized(header: str | None) -> bool:
    """A bearer token matching EDDIE_API_TOKEN. With no token set, nothing off loopback may write."""
    token = os.environ.get("EDDIE_API_TOKEN", "")
    scheme, _, value = (header or "").partition(" ")
    return bool(token) and scheme.lower() == "bearer" and hmac.compare_digest(value.strip().encode(), token.encode())


def allowed_origin(origin: str | None, host: str | None) -> bool:
    """No Origin (not a browser), a loopback page, or a page on the host this request names.

    The host must be an IP, localhost or .local, never a domain that could rebind to this machine.
    """
    if not origin:
        return True
    try:
        page, ours = urlsplit(origin).hostname, urlsplit("//" + (host or "")).hostname
    except ValueError:
        return False
    if page is None:  # "null": a sandboxed frame or a file
        return False
    if page == "localhost" or is_loopback(page):
        return True
    if page != ours:
        return False
    try:
        ipaddress.ip_address(page)
        return True
    except ValueError:
        return page.rstrip(".").endswith(".local")


@app.middleware("http")
async def guard_writes(request: Request, call_next):
    if request.method in WRITES:
        if not allowed_origin(request.headers.get("origin"), request.headers.get("host")):
            return JSONResponse({"detail": "Writes only come from Eddie's own pages."}, 403)
        client = request.client.host if request.client else None
        if not is_loopback(client) and not authorized(request.headers.get("authorization")):
            return JSONResponse({"detail": "Send Authorization: Bearer <EDDIE_API_TOKEN>."}, 401)
    return await call_next(request)


def feed() -> deque[dict]:
    """The live feed, after dropping events older than the retention window.

    Events arrive roughly in time order, so trimming from the oldest end is enough.
    Called on every read and ingest, so history expires even while the network is idle.
    """
    cutoff = int(time.time() * 1000) - FEED_RETENTION_MS
    while recent and recent[0]["ts"] < cutoff:
        del recent[0]
    return recent


async def broadcast(msg: dict) -> None:
    data = json.dumps(msg)
    for ws in list(clients):
        try:
            await ws.send_text(data)
        except Exception:
            clients.discard(ws)


@app.post("/ingest")
async def ingest(body: QueryEvent | list[QueryEvent]):
    events = body if isinstance(body, list) else [body]
    cutoff = int(time.time() * 1000) - FEED_RETENTION_MS
    feed()
    for ev in events:
        company, category = labels.lookup(ev.domain)
        every = timing["beacons"].observe(ev.device_mac, ev.domain, ev.ts)
        idle = timing["idle"].observe(ev.device_mac, ev.ts, ev.action == "allow" and category in labels.HUMAN)
        row = {**ev.model_dump(), "id": next(ids), "company": company, "category": category,
               "beacon_s": every and round(every / 1000), "idle": idle}
        if ev.ts >= cutoff:  # already past retention: still counted and broadcast, never kept
            recent.append(row)
        totals["seen"] += 1
        totals["blocked"] += ev.action == "block"
        await broadcast(row)
    await broadcast({"type": "totals", **totals})
    return {"ok": True, "count": len(events)}


@app.post('/ai/verdict')
async def ai_verdict(body: AIVerdictEvent):
    # Metadata only: neither rewrite history nor count an AI result as a query.
    event = {'type': 'ai_verdict', **body.model_dump()}
    if body.domain not in ai_verdicts and len(ai_verdicts) >= 1000:
        ai_verdicts.pop(next(iter(ai_verdicts)))
    ai_verdicts[body.domain] = event
    await broadcast(event)
    return {'ok': True}


@app.get('/ai/verdicts')
def cached_ai_verdicts():
    return list(ai_verdicts.values())


@app.get("/events")
def events(since: int = 0, device: str | None = None, limit: int = 200,
           action: Action | None = None, domain: str | None = None):
    rows = [e for e in feed() if e["ts"] > since and (device is None or e["device"] == device)
            and (action is None or e["action"] == action) and (domain is None or e["domain"] == domain)]
    return rows[-limit:]


@app.get("/summary")
def summary(window: Literal["15m", "1h", "24h"] = "1h"):
    events = list(feed())
    # text and companies cover the last hour; devices covers the requested window.
    return {**totals, "home_mode": state["home_mode"], "text": insights.summary_text(events),
            "companies": insights.company_counts(events), "company_window_seconds": 3600,
            "window": window, "devices": insights.device_counts(events, insights.WINDOWS_MS[window])}


@app.get("/what-they-know")
def what_they_know(hours: int = Query(24, ge=1, le=168), demo: bool = False):
    if demo:  # the seeded week, generated here and never mixed with real traffic
        facts = inferences.compute_facts(demo_week.events(int(time.time() * 1000)), 168)
        return {"demo": True, "label": demo_week.LABEL, "hours": 168, "facts": facts,
                "text": f"{demo_week.LABEL} {inferences.spoken_text(facts, 168)}"}
    events, source = report_events(hours)
    facts = inferences.compute_facts(events, hours)
    return {"demo": False, "source": source, "hours": hours, "facts": facts,
            "text": inferences.spoken_text(facts, hours)}


def report_events(hours: int) -> tuple[list[dict], str]:
    """The DNS server's query log when it has lookups in the window, else the in-memory feed.

    Runs in FastAPI's thread pool on its own read-only connection, so a week
    of rows doesn't stall the event loop or share the rules connection.
    """
    path = os.environ.get("EDDIE_DB", "eddie.db")
    if path != "off" and os.path.exists(path):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                events = inferences.events_from_db(conn, int(time.time() * 1000) - hours * 3_600_000)
            finally:
                conn.close()
            if events:
                return events, "eddie.db"
        except sqlite3.Error:
            pass  # no queries table yet, or unreadable: the feed is all there is
    return list(feed()), "feed"


@app.get("/mode")
async def get_mode():  # async: the shared connection stays on the event loop thread
    return {"home_mode": state["home_mode"], **offline_state()}


@app.post("/mode")
async def set_mode(body: ModeUpdate):
    state["home_mode"] = body.home_mode
    await broadcast({"type": "mode", "home_mode": body.home_mode})
    return {"home_mode": body.home_mode}


def rules_db() -> sqlite3.Connection:
    if db["conn"] is None:
        path = os.environ.get("EDDIE_DB", "eddie.db")
        if path == "off":
            raise HTTPException(503, "Rules and devices need eddie.db, and EDDIE_DB is off.")
        db["conn"] = store.connect(path)
    return db["conn"]


async def wait_applied(conn: sqlite3.Connection, rev: int, loaded=rules.loaded) -> bool:
    """True once the DNS engine has loaded revision rev; False if it doesn't within APPLY_TIMEOUT."""
    deadline = time.monotonic() + APPLY_TIMEOUT
    while loaded(conn) < rev:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(APPLY_POLL)
    return True


def named(conn: sqlite3.Connection, rule: dict) -> dict:
    """The rule plus its device's display name (None for a rule on every device)."""
    return {**rule, "device": rule["device_mac"] and devices.name_of(conn, rule["device_mac"])}


async def rule_change(conn: sqlite3.Connection, change: str, row: dict) -> None:
    # Sent once the row is stored or deleted, before DNS loads it: it says nothing about enforcement.
    await broadcast({"type": "rule_change", "change": change, **named(conn, row)})


def offline_state() -> dict:
    """offline: the state last asked for. offline_applied: whether DNS has loaded it."""
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        return {"offline": False, "offline_applied": False}
    conn = rules_db()
    return {"offline": offline.wanted(conn), "offline_applied": offline.loaded(conn) >= offline.revision(conn)}


@app.post("/offline-mode")
async def set_offline_mode(body: OfflineUpdate):
    conn = rules_db()
    rev = offline.write(conn, body.offline)
    applied = await wait_applied(conn, rev, offline.loaded)
    await broadcast({"type": "offline_mode", "offline": body.offline, "applied": applied})
    return {"offline": body.offline, "applied": applied}


@app.get("/rules")
async def list_rules():  # async: the shared connection stays on the event loop thread
    conn = rules_db()
    return [named(conn, r) for r in rules.all_rules(conn)]


@app.post("/rules", status_code=201)
async def add_rule(body: rules.RuleIn):
    conn = rules_db()
    row, rev = rules.add(conn, body)
    await rule_change(conn, "added", row)
    return {**row, "applied": await wait_applied(conn, rev)}


@app.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    conn = rules_db()
    row, rev = rules.remove(conn, rule_id)
    if row is None:
        raise HTTPException(404, f"No rule {rule_id}.")
    await rule_change(conn, "removed", row)
    return {**row, "deleted": True, "applied": await wait_applied(conn, rev)}


class LabelIn(BaseModel):
    label: str | None  # None or "" clears it

    @field_validator("label")
    @classmethod
    def check_label(cls, v: str | None) -> str | None:
        return devices.clean_label(v)


@app.get("/devices")
async def list_devices(hours: int = Query(24, ge=1, le=168)):
    since = int(time.time() * 1000) - hours * 3_600_000
    return {"hours": hours, "devices": devices.all_devices(rules_db(), since)}


@app.put("/devices/{key}")
async def label_device(key: str, body: LabelIn):
    key = key.strip().lower()
    if not rules.DEVICE.fullmatch(key):
        raise HTTPException(422, "Expected a MAC address or client IP.")
    row = devices.set_label(rules_db(), key, body.label)
    if row is None:
        raise HTTPException(404, f"Eddie hasn't seen a device {key}.")
    return {**row, "name": devices.display_name(key, row["label"], row["hostname"], row["vendor"])}


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    if not allowed_origin(ws.headers.get("origin"), ws.headers.get("host")):
        await ws.close(code=1008)  # before accept, so the handshake gets 403
        return
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "snapshot",
        "events": list(feed())[-60:],
        "ai_verdicts": list(ai_verdicts.values()),
        "totals": totals,
        "home_mode": state["home_mode"],
    }))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
