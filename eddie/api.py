"""Traffic feed: takes events in on /ingest and pushes them out on /ws/events.

The fake generator (scripts/fake_traffic.py) and the DNS server (eddie/feed.py)
both POST here; Codex's frontend listens on the WebSocket. A lookup of an
encrypted DNS resolver's name also pushes a bypass_suspected event (bypass.py),
and a blocked, beaconing lookup of a smart-TV ACR domain an acr_detected event (acr.py).

/rules writes user rules to eddie.db (EDDIE_DB, the DNS server's database) and
waits for the DNS server to load them before answering; see docs/EVENTS.md.
/devices lists the devices the DNS server recorded there, with lookup counts
from its query log, and stores user labels and the kid flag (devices.py).
/questions holds "block this likely tracker?" questions from AI tracker verdicts
in memory, and an answer writes a rule like /rules does (questions.py, #79).
/geo/alerts takes the DNS server's geo_alert events (geoip.py, #102) and
pushes them out, keeping the last 50 for the snapshot.
/devices/new takes the DNS server's new_device alerts and pushes them to the
feed; /quarantine and /devices/{key}/approve|deny change quarantine (#98,
quarantine.py) and wait for DNS to load it like rules.
/offline-mode turns the DNS server's upstream forwarding off or on through
eddie.db and waits for DNS to load it the same way (offline.py).
POST /mode {"away": ...} does the same for away mode (away.py), after
ranking which devices it covers from the query log.
POST /mode {"home_mode": ...} does it for home mode (home.py, #20) as a manual
change that holds until {"home_auto": true}; until then watch_presence turns
home mode on and off as marked household phones come and go.
POST /mode {"spoof_mode": ...} stores global tracker spoofing and waits for
the DNS process to load it before reporting `spoof_applied`.
/sensitivity turns sensitivity modes (#110, sensitivity.py) on or off and waits
for DNS to load the change the same way.
Each new block rule is watched for signs it broke a device and deleted
through DELETE /rules if it looks like it did (#101, rollback.py).
/ingest also runs each event through the device's baseline in eddie.db and
pushes new_domain and baseline_state events (baselines.py); /baselines and
/devices/{key}/baseline read and reset them.
/tracker-map says which tracking companies each device looked up (#107), and
/tracker-map/block blocks one of their ad-ID sync domains through /rules.

Writes (POST, PUT, DELETE) from anywhere but loopback need EDDIE_API_TOKEN as
a bearer token (#96). Browsers may only write or open /ws/events from this
machine's own pages; see allowed_origin.

Run: uv run uvicorn eddie.api:app --host 0.0.0.0 --port 8000
"""
import asyncio
import contextlib
import hmac
import ipaddress
import itertools
import json
import logging
import os
import sqlite3
import time
from collections import deque
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool, field_validator

from . import acr, away, coach, baselines, beacons, bypass, decision, demo_week, devices, home, inferences, insights, labels, nft, occupancy, offline, quarantine, questions, rollback, rules, sensitivity, spoof_mode, store
from .events import Action, AIVerdictEvent, GeoAlertEvent, ModeUpdate, NewDeviceEvent, OfflineUpdate, QuarantineUpdate, QueryEvent, SensitivityUpdate

# In memory for now; SQLite (store.py) replaces this once the DNS side lands.
recent: deque[dict] = deque(maxlen=20000)
# The feed keeps no more history than eddie.db does (#129).
FEED_RETENTION_MS = int(float(os.environ.get("EDDIE_RETENTION_DAYS", store.RETENTION_DAYS)) * 86_400_000)
FORGET_MS = int(store.FORGET_HOURS * 3_600_000)  # and no sensitive lookup past a day, like eddie.db (#111)
FORGET_EVERY_MS = 60_000  # at most one sensitive sweep of the feed a minute
totals = {"seen": 0, "blocked": 0}
forget_state = {"swept": 0}  # when feed() last dropped day-old sensitive events
state = {"home_mode": False}  # home mode as last read or written; eddie.db holds it (home.py) unless EDDIE_DB is off
clients: set[WebSocket] = set()
ai_verdicts: dict[str, dict] = {}
asking = questions.Questions()  # ask before blocking (#79)
timing = {"beacons": beacons.Beacons(), "idle": beacons.Idle()}  # beacon_s and idle on each event (#24)
suspicions = bypass.Suspicions()  # bypass_suspected events (#97)
bypass_recent: deque[dict] = deque(maxlen=50)
geo_recent: deque[dict] = deque(maxlen=50)  # geo_alert events (#102), memory only
acr_sightings = acr.Sightings()  # acr_detected events (#104)
acr_recent: deque[dict] = deque(maxlen=20)
watcher = rollback.Watcher()  # new block rules that may have broken a device (#101)
rollbacks: set[asyncio.Task] = set()  # rollbacks waiting for DNS, so ingest doesn't
ids = itertools.count(1)
baseline_settings = baselines.settings_from_env()  # (lookups, ms) to learn before new_domain events
log = logging.getLogger("eddie.api")
db: dict[str, sqlite3.Connection | None] = {"conn": None}
APPLY_TIMEOUT = 2.0  # seconds to wait for DNS to load a rule or offline change (it polls every 0.2 s)
APPLY_POLL = 0.02
PRESENCE_EVERY = 15.0  # seconds between household phone presence checks (#20)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        yield
        return
    nft.ensure_table()  # a reboot wipes the kernel table that home mode and router mode need
    home_state()
    if state["home_mode"]:
        nft.apply_home_mode(True, rules_db())
    task = asyncio.create_task(watch_presence())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Eddie traffic", lifespan=lifespan)
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
    Sensitive events (labels.SENSITIVE) older than a day are dropped from the
    middle too, in a sweep that runs at most once a minute.
    """
    now = int(time.time() * 1000)
    cutoff = now - FEED_RETENTION_MS
    while recent and recent[0]["ts"] < cutoff:
        del recent[0]
    if now - forget_state["swept"] >= FORGET_EVERY_MS:
        forget_state["swept"] = now
        kept = [e for e in recent if e["ts"] >= now - FORGET_MS or e.get("category") not in labels.SENSITIVE]
        if len(kept) < len(recent):
            recent.clear()
            recent.extend(kept)
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
    cutoff_sensitive = int(time.time() * 1000) - FORGET_MS
    feed()
    for ev in events:
        company, category = labels.lookup(ev.domain)
        every = timing["beacons"].observe(ev.device_mac, ev.domain, ev.ts)
        idle = timing["idle"].observe(ev.device_mac, ev.ts, ev.action == "allow" and category in labels.HUMAN)
        seller = labels.broker(ev.domain)
        row = {**ev.model_dump(), "id": next(ids), "company": company, "category": category,
               "broker": seller and seller["kind"], "beacon_s": every and round(every / 1000), "idle": idle}
        forgotten = category in labels.SENSITIVE and ev.ts < cutoff_sensitive
        if ev.ts >= cutoff and not forgotten:  # already past retention: still counted and broadcast, never kept
            recent.append(row)
        totals["seen"] += 1
        totals["blocked"] += ev.action == "block"
        await broadcast(row)
        if suspect := suspicions.observe(row):
            bypass_recent.append(suspect)
            await broadcast(suspect)
        lookups = len(timing["beacons"].seen.get((ev.device_mac, ev.domain), ()))
        if sighting := acr_sightings.observe(row, lookups):
            acr_recent.append(sighting)
            await broadcast(sighting)
        for found in watcher.observe(row):
            task = asyncio.create_task(roll_back(found))
            rollbacks.add(task)
            task.add_done_callback(rollbacks.discard)
    for notice in baseline_events([ev for ev in events if ev.ts >= cutoff]):
        await broadcast(notice)
    await broadcast({"type": "totals", **totals})
    await ask_due()
    return {"ok": True, "count": len(events)}


def baseline_events(events: list[QueryEvent]) -> list[dict]:
    """Add the batch to each device's baseline in one transaction; the events that causes.

    Nothing with EDDIE_DB=off. A database error loses the batch's baseline
    updates, never the feed.
    """
    if os.environ.get("EDDIE_DB", "eddie.db") == "off" or not events:
        return []
    conn = rules_db()
    out = []
    try:
        with conn:
            for ev in events:
                out += baselines.observe(conn, ev.model_dump(), *baseline_settings)
    except sqlite3.Error as exc:
        log.warning("baseline update failed (%s), %d events skipped", exc, len(events))
        return []
    return out


@app.post('/ai/verdict')
async def ai_verdict(body: AIVerdictEvent):
    # Metadata only: neither rewrite history nor count an AI result as a query.
    event = {'type': 'ai_verdict', **body.model_dump()}
    if body.domain not in ai_verdicts and len(ai_verdicts) >= 1000:
        ai_verdicts.pop(next(iter(ai_verdicts)))
    ai_verdicts[body.domain] = event
    await broadcast(event)
    if body.label == 'tracker' and not decision.is_plumbing(body.domain):
        # The device that looked it up most recently names the question; the rule covers every device.
        device = next((e['device'] for e in reversed(feed()) if e['domain'] == body.domain), None)
        asking.offer(body.domain, body.reason, device)
        await ask_due()
    return {'ok': True}


async def ask_due() -> None:
    question = asking.release()
    if question:
        await broadcast({'type': 'question', **question})


class AnswerIn(BaseModel):
    answer: Literal['block', 'allow', 'ignore']
    by: Literal['dashboard', 'voice']


@app.get('/questions')
async def open_questions():
    await ask_due()
    return asking.pending()


@app.post('/questions/{question_id}')
async def answer_question(question_id: int, body: AnswerIn):
    """Block or Allow writes one global rule per domain (created_by: body.by); Ignore writes none.

    applied is DNS's acknowledgment of the last rule, as in POST /rules (null for Ignore).
    """
    conn = None if body.answer == 'ignore' else rules_db()
    question = asking.take(question_id)
    if question is None:
        raise HTTPException(404, f'No open question {question_id}.')
    added, rev = [], None
    try:
        for item in question['domains'] if conn else []:
            row, rev = rules.add(conn, rules.RuleIn(pattern=item['domain'], action=body.answer, created_by=body.by))
            added.append(row)
            watcher.watch(row)  # a block from an answer can be rolled back like any other (#101)
            await rule_change(conn, 'added', row)
    except Exception:
        if not added:
            asking.open[question_id] = question  # nothing changed: it can be answered again
        raise
    applied = None if rev is None else await wait_applied(conn, rev)
    await broadcast({'type': 'question_resolved', 'id': question_id, 'answer': body.answer, 'by': body.by,
                     'applied': applied})
    await ask_due()
    return {'id': question_id, 'answer': body.answer, 'rules': added, 'applied': applied}


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
    # text and companies cover the last hour; devices and brokers cover the requested window.
    return {**totals, "home_mode": state["home_mode"], "text": insights.summary_text(events),
            "companies": insights.company_counts(events), "company_window_seconds": 3600,
            "window": window, "devices": insights.device_counts(events, insights.WINDOWS_MS[window]),
            "brokers": insights.broker_counts(events, insights.WINDOWS_MS[window])}


class SyncBlockIn(BaseModel):
    domain: str  # an ad-ID sync domain from GET /tracker-map


def sync_rule(conn: sqlite3.Connection, domain: str) -> dict | None:
    """The every-device block rule already covering an ad-ID sync domain, if any."""
    return next((r for r in rules.all_rules(conn) if r["action"] == "block" and r["device_mac"] is None
                 and r["pattern"] in (domain, f"*.{domain}")), None)


@app.get("/tracker-map")
async def tracker_map(window: Literal["1h", "24h"] = "24h"):
    """Which companies see which devices (#107). Each id_sync entry says whether a block rule
    covers it; a rule is stored intent, not proof DNS is enforcing it."""
    result = insights.tracker_map(list(feed()), insights.WINDOWS_MS[window])
    try:
        conn = rules_db()
    except HTTPException:
        conn = None  # EDDIE_DB=off: the map still shows, with no rule state
    for row in result["companies"]:
        for sync in row["id_sync"]:
            rule = conn and sync_rule(conn, sync["domain"])
            sync["rule_id"] = rule["id"] if rule else None
    return {"window": window, **result}


@app.post("/tracker-map/block")
async def block_id_sync(body: SyncBlockIn):
    """Block an ad-ID sync domain and its subdomains on every device, through POST /rules."""
    domain = body.domain.strip().lower().rstrip(".")
    if labels.id_sync(domain) != domain:
        raise HTTPException(422, "Only ad-ID sync domains from the tracker map can be blocked here.")
    conn = rules_db()
    if existing := sync_rule(conn, domain):
        return {**named(conn, existing), "existing": True, "applied": None}
    return {**await add_rule(rules.RuleIn(pattern=f"*.{domain}", action="block", created_by="dashboard")),
            "existing": False}


def report_limit_hours() -> int:
    """The longest honest report is the configured on-device retention window."""
    try:
        days = float(os.environ.get("EDDIE_RETENTION_DAYS", store.RETENTION_DAYS))
    except ValueError:
        days = store.RETENTION_DAYS
    return max(1, int(days * 24))


@app.get("/what-they-know")
def what_they_know(hours: int | None = Query(None, ge=1, le=8760), demo: bool = False):
    if demo:  # the seeded week, generated here and never mixed with real traffic
        facts = inferences.compute_facts(demo_week.events(int(time.time() * 1000)), 168)
        return {"demo": True, "label": demo_week.LABEL, "hours": 168, "facts": facts,
                "text": f"{demo_week.LABEL} {inferences.spoken_text(facts, 168)}"}
    limit = report_limit_hours()
    requested = hours or limit
    actual = min(requested, limit)
    events, source = report_events(actual)
    facts = inferences.compute_facts(events, actual)
    quality = inferences.data_quality(events, actual)
    limited = requested > actual
    notice = (f"I only retain the last {inferences.period(actual)}, so I used all available data."
              if limited else None)
    return {"demo": False, "source": source, "requested_hours": requested, "hours": actual,
            "limited_by_retention": limited, "notice": notice, "facts": facts, **quality,
            "text": " ".join(filter(None, (notice, inferences.spoken_text(facts, actual, quality["enough_data"]))))}


@app.get("/burglars-eye")
def burglars_eye(hours: int | None = Query(None, ge=1, le=8760), demo: bool = False):
    """When could someone tell the house was empty (#34); see occupancy.py. A week unless asked."""
    if demo:
        report = occupancy.report(demo_week.events(int(time.time() * 1000)), 168)
        return {"demo": True, "label": demo_week.LABEL, **report, "text": f"{demo_week.LABEL} {report['text']}"}
    requested = hours or occupancy.WEEK
    actual = min(requested, report_limit_hours())
    events, source = report_events(actual)
    report = occupancy.report(events, actual)
    limited = requested > actual
    notice = (f"I only retain the last {inferences.period(actual)}, so I used all available data."
              if limited else None)
    return {"demo": False, "source": source, **report, "requested_hours": requested,
            "limited_by_retention": limited, "notice": notice,
            "text": " ".join(filter(None, (notice, report["text"])))}


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
    return {**home_state(), **spoof_state(), **offline_state(), **away_state(), **quarantine_state()}


@app.post("/mode")
async def set_mode(body: ModeUpdate):
    result = {}
    if body.away is not None:  # first: it can fail (no eddie.db), and then nothing changes
        result.update(await set_away(body.away, body.away_until))
    if body.home_mode is not None:
        result.update(await set_home(body.home_mode, True, "manual"))
    if body.spoof_mode is not None:
        result.update(await set_spoof(body.spoof_mode))
    if body.home_auto:
        conn = rules_db()
        result.update(await set_home(bool(home.phones_home(conn, int(time.time() * 1000))), False, "presence"))
    return result


def home_state() -> dict:
    """home_mode: the state last asked for. home_manual: whether it overrides presence. home_applied: whether DNS has loaded it."""
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        return {"home_mode": state["home_mode"], "home_manual": False, "home_applied": False}
    conn = rules_db()
    state["home_mode"] = home.wanted(conn)
    return {"home_mode": state["home_mode"], "home_manual": home.manual(conn),
            "home_applied": home.loaded(conn) >= home.revision(conn)}


def spoof_state() -> dict:
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        return {"spoof_mode": False, "spoof_applied": False}
    conn = rules_db()
    return {"spoof_mode": spoof_mode.wanted(conn),
            "spoof_applied": spoof_mode.loaded(conn) >= spoof_mode.revision(conn)}


async def set_spoof(on: bool) -> dict:
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        result = {"spoof_mode": on, "spoof_applied": False}
    else:
        conn = rules_db()
        rev = spoof_mode.write(conn, on)
        result = {"spoof_mode": on, "spoof_applied": await wait_applied(conn, rev, spoof_mode.loaded)}
    await broadcast({"type": "mode", **result, "reason": "voice"})
    return result


async def set_home(on: bool, manual: bool, reason: str) -> dict:
    """Store home mode, wait for DNS to load it, then send the mode event with whether it did."""
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":  # nothing can enforce it: a flag in memory
        state["home_mode"] = on
        result = {"home_mode": on, "home_manual": manual, "home_applied": False}
    else:
        conn = rules_db()
        rev = home.write(conn, on, manual)
        state["home_mode"] = on
        nft.apply_home_mode(on, conn)
        result = {"home_mode": on, "home_manual": manual, "home_applied": await wait_applied(conn, rev, home.loaded)}
    await broadcast({"type": "mode", **result, "reason": reason})
    return result


async def check_presence(now_ms: int | None = None) -> dict | None:
    """Turn home mode on when a marked phone is home and off when none is, unless a manual change holds.

    Returns set_home's result, or None if nothing changed. No await before the write,
    so a manual change can't land between the check and it.
    """
    conn = rules_db()
    on = bool(home.phones_home(conn, int(time.time() * 1000) if now_ms is None else now_ms))
    if home.manual(conn) or on == home.wanted(conn):
        return None
    return await set_home(on, False, "presence")


async def watch_presence(every: float = PRESENCE_EVERY) -> None:
    while True:
        try:
            await check_presence()
        except (sqlite3.Error, HTTPException) as exc:
            log.warning("home mode presence check failed (%s)", getattr(exc, "detail", exc))
        await asyncio.sleep(every)


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


def quarantine_state() -> dict:
    """quarantine: the state last asked for. quarantine_applied: whether DNS has loaded every quarantine change."""
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        return {"quarantine": False, "quarantine_applied": False}
    conn = rules_db()
    return {"quarantine": quarantine.wanted(conn),
            "quarantine_applied": quarantine.loaded(conn) >= quarantine.revision(conn)}


@app.post("/quarantine")
async def set_quarantine(body: QuarantineUpdate):
    conn = rules_db()
    rev = quarantine.write(conn, body.quarantine)
    applied = await wait_applied(conn, rev, quarantine.loaded)
    await broadcast({"type": "quarantine", "quarantine": body.quarantine, "applied": applied})
    return {"quarantine": body.quarantine, "applied": applied}


def sensitivity_state() -> dict:
    """protected: the modes last turned on. applied: whether DNS has loaded every change. modes: each rule set."""
    conn = rules_db()
    on = sensitivity.wanted(conn)
    return {"protected": on, "applied": sensitivity.loaded(conn) >= sensitivity.revision(conn),
            "modes": [{**sensitivity.describe(m), "on": m in on} for m in sensitivity.MODES]}


@app.get("/sensitivity")
async def get_sensitivity():  # async: the shared connection stays on the event loop thread
    return sensitivity_state()


@app.post("/sensitivity")
async def set_sensitivity(body: SensitivityUpdate):
    conn = rules_db()
    rev = sensitivity.write(conn, body.mode, body.on)
    result = {**sensitivity.describe(body.mode), "on": body.on, "protected": sensitivity.wanted(conn),
              "applied": await wait_applied(conn, rev, sensitivity.loaded)}
    await broadcast({"type": "sensitivity", **result})
    return result


@app.post("/offline-mode")
async def set_offline_mode(body: OfflineUpdate):
    conn = rules_db()
    rev = offline.write(conn, body.offline)
    applied = await wait_applied(conn, rev, offline.loaded)
    await broadcast({"type": "offline_mode", "offline": body.offline, "applied": applied})
    return {"offline": body.offline, "applied": applied}


def away_state() -> dict:
    """away: on and not past away_until. away_applied: whether DNS has loaded the last change."""
    if os.environ.get("EDDIE_DB", "eddie.db") == "off":
        return {"away": False, "away_until": None, "away_applied": False, "away_devices": [], "away_exempt": []}
    conn = rules_db()
    on = away.active(conn, int(time.time() * 1000))
    covered, exempt = away.stored(conn) if on else ([], [])
    return {"away": on, "away_until": (away.until(conn) or None) if on else None,
            "away_applied": away.loaded(conn) >= away.revision(conn), "away_devices": covered, "away_exempt": exempt}


def away_ranking() -> tuple[list[dict], list[dict]]:
    """away.rank on its own read-only connection, for a worker thread: a week of rows."""
    conn = sqlite3.connect(f"file:{os.environ.get('EDDIE_DB', 'eddie.db')}?mode=ro", uri=True)
    try:
        return away.rank(conn, int(time.time() * 1000))
    finally:
        conn.close()


async def set_away(on: bool, until_ms: int | None) -> dict:
    conn = rules_db()
    covered, exempt = await asyncio.to_thread(away_ranking) if on else ([], [])
    rev = away.write(conn, on, until_ms, covered, exempt)
    result = {"away": on, "away_until": until_ms if on else None,
              "away_applied": await wait_applied(conn, rev, away.loaded),
              "away_devices": covered, "away_exempt": exempt}
    await broadcast({"type": "away_mode", **result})
    return result


@app.get("/rules")
async def list_rules():  # async: the shared connection stays on the event loop thread
    conn = rules_db()
    return [named(conn, r) for r in rules.all_rules(conn)]


@app.post("/rules", status_code=201)
async def add_rule(body: rules.RuleIn):
    conn = rules_db()
    row, rev = rules.add(conn, body)
    watcher.watch(row)
    await rule_change(conn, "added", row)
    return {**row, "applied": await wait_applied(conn, rev)}


@app.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    conn = rules_db()
    row, rev = rules.remove(conn, rule_id)
    if row is None:
        raise HTTPException(404, f"No rule {rule_id}.")
    watcher.forget(rule_id)
    await rule_change(conn, "removed", row)
    return {**row, "deleted": True, "applied": await wait_applied(conn, rev)}


@app.post("/rules/{rule_id}/pin")
async def pin_rule(rule_id: int):
    """Keep a rule however its device behaves: it is never rolled back (#101)."""
    conn = rules_db()
    row = rules.pin(conn, rule_id)
    if row is None:
        raise HTTPException(404, f"No rule {rule_id}.")
    watcher.forget(rule_id)
    return named(conn, row)


async def roll_back(found: rollback.Rollback) -> None:
    """Delete the rule through DELETE /rules, then say why. A guess, never proof (rollback.py)."""
    current = rules.get(rules_db(), found.rule["id"])
    if current is None or current["pinned"] or current["ts"] != found.rule["ts"]:
        return  # deleted, pinned meanwhile, or the id now holds another rule: nothing to roll back
    out = await delete_rule(found.rule["id"])
    await broadcast({"type": "rule_rolled_back", "ts": int(time.time() * 1000),
                     "rule": named(rules_db(), found.rule), "device_mac": found.device_mac,
                     "device": found.device, "sign": found.sign, "reason": found.reason,
                     "applied": out["applied"]})


class DeviceIn(BaseModel):
    label: str | None = None  # None or "" clears it
    kid: StrictBool | None = None  # a child's device, for kid mode (#119)
    phone: StrictBool | None = None  # a household phone, for home mode's presence check (#20)
    security: StrictBool | None = None  # a security/outdoor device, exempt from home mode (#46)

    @field_validator("label")
    @classmethod
    def check_label(cls, v: str | None) -> str | None:
        return devices.clean_label(v)


@app.get("/devices")
async def list_devices(hours: int = Query(24, ge=1, le=168)):
    since = int(time.time() * 1000) - hours * 3_600_000
    return {"hours": hours, "devices": devices.all_devices(rules_db(), since), **quarantine_state()}


def coach_devices() -> list[dict]:
    """coach.suggestions on its own read-only connection, for a worker thread: a week of rows."""
    conn = sqlite3.connect(f"file:{os.environ.get('EDDIE_DB', 'eddie.db')}?mode=ro", uri=True)
    try:
        return coach.suggestions(conn, int(time.time() * 1000) - coach.WEEK_MS)
    finally:
        conn.close()


@app.get("/suggestions")
async def suggestions():
    """Privacy settings to change on recognised devices (#112); guidance only, Eddie changes nothing."""
    path = os.environ.get("EDDIE_DB", "eddie.db")
    if path == "off" or not os.path.exists(path):
        raise HTTPException(503, "Device log unavailable.")
    try:
        found = await asyncio.to_thread(coach_devices)
    except sqlite3.Error:
        raise HTTPException(503, "Device log unavailable.")
    return {"devices": [{**d, "guide": coach.GUIDES[d["kind"]]} for d in found],
            "guides": [g["name"] for g in coach.GUIDES.values()], "text": coach.spoken_text(found)}


@app.post("/geo/alerts")
async def geo_alerts(body: GeoAlertEvent | list[GeoAlertEvent]):
    # Not a lookup: totals and the feed are unchanged. A restart forgets them.
    alerts = body if isinstance(body, list) else [body]
    for alert in alerts:
        event = alert.model_dump()
        geo_recent.append(event)
        await broadcast(event)
    return {"ok": True, "count": len(alerts)}


@app.post("/devices/new")
async def new_devices(body: NewDeviceEvent | list[NewDeviceEvent]):
    # Sent by the DNS server once the MAC is stored as pending; says nothing about enforcement.
    alerts = body if isinstance(body, list) else [body]
    for alert in alerts:
        await broadcast(alert.model_dump())
    return {"ok": True, "count": len(alerts)}


async def decide_device(key: str, status: str) -> dict:
    key = key.strip().lower()
    if devices.normalize_mac(key) != key:
        raise HTTPException(422, "Expected a MAC address: a device known only by its IP can't be approved or denied.")
    conn = rules_db()
    row, rev = quarantine.decide(conn, key, status)
    if row is None:
        raise HTTPException(404, f"Eddie hasn't seen a device {key}.")
    applied = await wait_applied(conn, rev, quarantine.loaded)
    out = {"mac": key, "name": devices.name_of(conn, key), "status": row["status"], "applied": applied}
    await broadcast({"type": "device_status", **out})
    return out


@app.post("/devices/{key}/approve")
async def approve_device(key: str):
    return await decide_device(key, quarantine.APPROVED)


@app.post("/devices/{key}/deny")
async def deny_device(key: str):
    return await decide_device(key, quarantine.DENIED)


@app.put("/devices/{key}")
async def update_device(key: str, body: DeviceIn):
    key = key.strip().lower()
    if not rules.DEVICE.fullmatch(key):
        raise HTTPException(422, "Expected a MAC address or client IP.")
    changes = body.model_dump(include=body.model_fields_set)
    for flag in ("kid", "phone", "security"):
        if changes.get(flag) is None:
            changes.pop(flag, None)
    if not changes:
        raise HTTPException(422, "Expected a label, kid, phone, or security.")
    row = devices.update(rules_db(), key, changes)
    if row is None:
        raise HTTPException(404, f"Eddie hasn't seen a device {key}.")
    return {**row, "name": devices.display_name(key, row["label"], row["hostname"], row["vendor"])}


def baseline_key(key: str) -> str:
    key = baselines.key_of(key.strip().lower())
    if not rules.DEVICE.fullmatch(key):
        raise HTTPException(422, "Expected a MAC address or client IP.")
    return key


def named_baseline(conn: sqlite3.Connection, key: str) -> dict | None:
    row = baselines.state(conn, key, int(time.time() * 1000), *baseline_settings)
    return row and {**row, "device": devices.name_of(conn, key)}


@app.get("/baselines")
async def list_baselines():
    conn = rules_db()
    return [named_baseline(conn, key) for key in baselines.all_keys(conn)]


@app.get("/devices/{key}/baseline")
async def get_baseline(key: str):
    key = baseline_key(key)
    row = named_baseline(rules_db(), key)
    if row is None:
        raise HTTPException(404, f"No baseline for {key} yet.")
    return row


@app.delete("/devices/{key}/baseline")
async def reset_baseline(key: str):
    key = baseline_key(key)
    conn = rules_db()
    deleted = baselines.reset(conn, key)
    if deleted is None:
        raise HTTPException(404, f"No baseline for {key} yet.")
    await broadcast({"type": "baseline_state", "ts": int(time.time() * 1000), "device_mac": key,
                     "device": devices.name_of(conn, key), "state": "reset", "domains": 0})
    return {"device_mac": key, "reset": True, "domains_deleted": deleted}


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
        "bypass": list(bypass_recent),
        "geo": list(geo_recent),
        "acr": list(acr_recent),
        "questions": asking.pending(),
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
