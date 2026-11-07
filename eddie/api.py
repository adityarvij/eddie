"""Traffic feed: takes events in on /ingest and pushes them out on /ws/events.

The fake generator (scripts/fake_traffic.py) and the DNS server (eddie/feed.py)
both POST here; Codex's frontend listens on the WebSocket.

/rules writes user rules to eddie.db (EDDIE_DB, the DNS server's database) and
waits for the DNS server to load them before answering; see docs/EVENTS.md.

Run: uv run uvicorn eddie.api:app --host 0.0.0.0 --port 8000
"""
import asyncio
import itertools
import json
import os
import sqlite3
import time
from collections import deque

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from . import inferences, insights, labels, rules, store
from .events import AIVerdictEvent, ModeUpdate, QueryEvent

# In memory for now; SQLite (store.py) replaces this once the DNS side lands.
recent: deque[dict] = deque(maxlen=20000)
totals = {"seen": 0, "blocked": 0}
state = {"home_mode": False}
clients: set[WebSocket] = set()
ai_verdicts: dict[str, dict] = {}
ids = itertools.count(1)
db: dict[str, sqlite3.Connection | None] = {"conn": None}
APPLY_TIMEOUT = 2.0  # seconds to wait for DNS to load a rule change (it polls every 0.2 s)
APPLY_POLL = 0.02

app = FastAPI(title="Eddie traffic")


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
    for ev in events:
        company, category = labels.lookup(ev.domain)
        row = {**ev.model_dump(), "id": next(ids), "company": company, "category": category}
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
def events(since: int = 0, device: str | None = None, limit: int = 200):
    rows = [e for e in recent if e["ts"] > since and (device is None or e["device"] == device)]
    return rows[-limit:]


@app.get("/summary")
def summary():
    return {**totals, "home_mode": state["home_mode"], "text": insights.summary_text(list(recent)),
            "companies": insights.company_counts(list(recent)), "company_window_seconds": 3600}


@app.get("/what-they-know")
def what_they_know(hours: int = Query(24, ge=1, le=168)):
    facts = inferences.compute_facts(list(recent), hours)
    return {"hours": hours, "facts": facts, "text": inferences.spoken_text(facts, hours)}


@app.get("/mode")
def get_mode():
    return {"home_mode": state["home_mode"]}


@app.post("/mode")
async def set_mode(body: ModeUpdate):
    state["home_mode"] = body.home_mode
    await broadcast({"type": "mode", "home_mode": body.home_mode})
    return {"home_mode": body.home_mode}


def rules_db() -> sqlite3.Connection:
    if db["conn"] is None:
        path = os.environ.get("EDDIE_DB", "eddie.db")
        if path == "off":
            raise HTTPException(503, "Rules need eddie.db, and EDDIE_DB is off.")
        db["conn"] = store.connect(path)
    return db["conn"]


async def wait_applied(conn: sqlite3.Connection, rev: int) -> bool:
    """True once the DNS engine has loaded revision rev; False if it doesn't within APPLY_TIMEOUT."""
    deadline = time.monotonic() + APPLY_TIMEOUT
    while rules.loaded(conn) < rev:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(APPLY_POLL)
    return True


@app.get("/rules")
async def list_rules():  # async: the shared connection stays on the event loop thread
    return rules.all_rules(rules_db())


@app.post("/rules", status_code=201)
async def add_rule(body: rules.RuleIn):
    conn = rules_db()
    row, rev = rules.add(conn, body)
    return {**row, "applied": await wait_applied(conn, rev)}


@app.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    conn = rules_db()
    row, rev = rules.remove(conn, rule_id)
    if row is None:
        raise HTTPException(404, f"No rule {rule_id}.")
    return {**row, "deleted": True, "applied": await wait_applied(conn, rev)}


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "snapshot",
        "events": list(recent)[-60:],
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
