"""Traffic feed: takes events in on /ingest and pushes them out on /ws/events.

The fake generator (scripts/fake_traffic.py) and later Will's DNS server both
POST here; Codex's frontend listens on the WebSocket.

Run: uv run uvicorn eddie.api:app --host 0.0.0.0 --port 8000
"""
import itertools
import json
from collections import deque

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect

from . import inferences, insights, labels
from .events import ModeUpdate, QueryEvent

# In memory for now; SQLite (store.py) replaces this once the DNS side lands.
recent: deque[dict] = deque(maxlen=20000)
totals = {"seen": 0, "blocked": 0}
state = {"home_mode": False}
clients: set[WebSocket] = set()
ids = itertools.count(1)

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


@app.get("/events")
def events(since: int = 0, device: str | None = None, limit: int = 200):
    rows = [e for e in recent if e["ts"] > since and (device is None or e["device"] == device)]
    return rows[-limit:]


@app.get("/summary")
def summary():
    return {**totals, "home_mode": state["home_mode"], "text": insights.summary_text(list(recent))}


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


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "snapshot",
        "events": list(recent)[-60:],
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
