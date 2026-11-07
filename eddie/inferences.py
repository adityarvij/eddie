""""What they know about you": household inferences from real traffic (served at /what-they-know).

Scaffold of README "Intelligence features → Priority 1". Code computes facts
from the event log; nothing here is invented. Each fact is
{fact, evidence, confidence} so a local model can later phrase them.

Built: company exposure, active hours, after-midnight sessions, idle
telemetry, beacons (a domain looked up on a timer), longest quiet stretch, and sessions (same category, gaps under
10 minutes) by weekday. Not built yet: camera/doorbell occupancy signals.
The local-model phrasing and its number check live in voice/ollama_agent.py
(phrase); spoken_text is the template used when the model is off or fails.

Events come from eddie.db's query log (events_from_db) when it has lookups
in the window, otherwise from the API's in-memory feed.
"""
import sqlite3
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime

from .beacons import Beacons
from .insights import plural
from .labels import HUMAN, TRACKING, lookup

HOUR_MS = 60 * 60 * 1000
IDLE_MS = 5 * 60 * 1000  # no human traffic for this long = nobody is using the device
LATE_HOURS = range(0, 5)  # midnight to 5 AM local
SESSION_GAP_MS = 10 * 60 * 1000  # same category, gaps under this = one session
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def clock(ts_ms: int, with_day: bool = False) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%a %-I:%M %p" if with_day else "%-I:%M %p")


def events_from_db(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """Query-log rows since since_ms as feed-shaped events, oldest first.

    The device is its user label, else "<vendor> device", else "unlabeled
    device": never a MAC, IP or DHCP hostname, since facts can reach the
    local model. Unlabeled devices without a known vendor share one name.
    """
    rows = conn.execute(
        "SELECT q.ts, q.domain, q.action, d.label, d.vendor FROM queries q"
        " LEFT JOIN devices d ON d.mac = q.device_mac WHERE q.ts >= ? ORDER BY q.ts", (since_ms,))
    events = []
    for ts, domain, action, label, vendor in rows:
        company, category = lookup(domain)
        device = label or (f"{vendor} device" if vendor else "unlabeled device")
        events.append({"ts": ts, "domain": domain, "action": action, "device": device,
                       "company": company, "category": category})
    return events


def minutes(ms: int) -> int:
    return round(ms / 60_000)


def sessions(events: list[dict], gap_ms: int = SESSION_GAP_MS) -> list[dict]:
    """Group requests into sessions: same category, each gap under gap_ms.

    Returns {category, start, end, requests, devices} sorted by start. A
    single request is a session with start == end.
    """
    open_by_category: dict[str, dict] = {}
    done = []
    for e in sorted(events, key=lambda e: e["ts"]):
        cur = open_by_category.get(e["category"])
        if cur is None or e["ts"] - cur["end"] >= gap_ms:
            if cur is not None:
                done.append(cur)
            cur = open_by_category[e["category"]] = {
                "category": e["category"], "start": e["ts"], "end": e["ts"], "requests": 0, "devices": set()}
        cur["end"] = e["ts"]
        cur["requests"] += 1
        cur["devices"].add(e["device"])
    done.extend(open_by_category.values())
    for s in done:
        s["devices"] = sorted(s["devices"])
    return sorted(done, key=lambda s: (s["start"], s["category"]))


def weekday_facts(human_sessions: list[dict]) -> list[dict]:
    """Category by weekday: which day each human category peaks, and weekdays vs weekends.

    Needs sessions on at least two calendar days; one day says nothing about weekdays.
    """
    facts = []
    by_category = defaultdict(list)
    for s in human_sessions:
        by_category[s["category"]].append(s)
    for category, group in sorted(by_category.items()):
        day_minutes = defaultdict(int)  # calendar date -> session minutes (at least 1 per session)
        for s in group:
            day_minutes[datetime.fromtimestamp(s["start"] / 1000).date()] += max(1, minutes(s["end"] - s["start"]))
        if len(day_minutes) < 2:
            continue
        per_weekday = defaultdict(int)
        for day, m in day_minutes.items():
            per_weekday[day.weekday()] += m
        total = sum(per_weekday.values())
        top, top_min = max(per_weekday.items(), key=lambda kv: (kv[1], -kv[0]))
        days = len(day_minutes)
        facts.append(fact(
            f"{category.capitalize()} happens most on {WEEKDAYS[top]}s",
            f"{top_min} of {total} {category} minutes in {plural(len(group), 'session')} "
            f"were on {WEEKDAYS[top]}, across {plural(days, 'day')} with {category}",
            min(0.7, 0.3 + 0.05 * days),
        ))
        weekend = [m for day, m in day_minutes.items() if day.weekday() >= 5]
        weekday = [m for day, m in day_minutes.items() if day.weekday() < 5]
        if weekend and weekday:
            weekend_avg, weekday_avg = sum(weekend) // len(weekend), sum(weekday) // len(weekday)
            if 2 * weekend_avg >= 3 * weekday_avg or 2 * weekday_avg >= 3 * weekend_avg:  # 1.5x either way
                more = "weekends" if weekend_avg > weekday_avg else "weekdays"
                facts.append(fact(
                    f"{category.capitalize()} is heavier on {more}",
                    f"{weekend_avg} minutes a weekend day ({plural(len(weekend), 'day')}) vs "
                    f"{weekday_avg} a weekday ({plural(len(weekday), 'day')})",
                    min(0.7, 0.3 + 0.05 * days),
                ))
    return facts


def fact(text: str, evidence: str, confidence: float) -> dict:
    return {"fact": text, "evidence": evidence, "confidence": round(confidence, 2)}


def compute_facts(events: list[dict], hours: int = 24, now_ms: int | None = None) -> list[dict]:
    now_ms = now_ms or int(time.time() * 1000)
    window = sorted((e for e in events if now_ms - e["ts"] < hours * HOUR_MS), key=lambda e: e["ts"])
    if not window:
        return []
    facts = []

    tracking = [e for e in window if e["category"] in TRACKING]
    by_company = Counter(e["company"] for e in tracking)
    if by_company:
        company, n = by_company.most_common(1)[0]
        blocked = sum(e["action"] == "block" for e in tracking if e["company"] == company)
        facts.append(fact(
            f"{company} gets the most tracking data from this house",
            f"{plural(n, 'tracking request')} to {company} in {hours}h, {blocked} blocked",
            0.9,
        ))

    human = [e for e in window if e["category"] in HUMAN]
    human_sessions = sessions(human)
    if human:
        facts.append(fact(
            "Someone was using a screen between these times",
            f"streaming traffic from {clock(human[0]['ts'], hours > 24)} to {clock(human[-1]['ts'], hours > 24)}",
            0.7,
        ))
        late = [s for s in human_sessions if datetime.fromtimestamp(s["start"] / 1000).hour in LATE_HOURS]
        if late:
            facts.append(fact(
                "Someone is up watching after midnight",
                f"{plural(len(late), 'streaming session')} started between midnight and 5 AM, "
                f"{plural(sum(s['requests'] for s in late), 'request')} in all",
                0.6,
            ))
        gaps = [(b["ts"] - a["ts"], a["ts"]) for a, b in zip(human, human[1:])]
        if gaps:
            gap, start = max(gaps)
            if gap >= HOUR_MS:
                facts.append(fact(
                    "The house may have been empty or asleep for a stretch",
                    f"no streaming for {gap // HOUR_MS}h starting {clock(start, hours > 24)}",
                    0.5,
                ))

    # Tracking while nobody is watching: the device reports on its own.
    human_ts = [e["ts"] for e in human]  # sorted, since window is

    def watched(ts: int) -> bool:
        i = bisect_right(human_ts, ts)
        return i > 0 and ts - human_ts[i - 1] < IDLE_MS

    idle = [e for e in tracking if not watched(e["ts"])]
    if idle:
        devices = Counter(e["device"] for e in idle)
        device, n = devices.most_common(1)[0]
        facts.append(fact(
            f"The {device} reports home even when nobody is using it",
            f"{plural(n, 'tracking request')} with no streaming in the 5 minutes before",
            0.8,
        ))
    if beacon := beacon_fact(window, hours):
        facts.append(beacon)
    facts.extend(weekday_facts(human_sessions))
    return facts


def beacon_fact(window: list[dict], hours: int) -> dict | None:
    """The device and domain looked up most often on a regular timer (beacons.py), listed or not."""
    tracker, timed = Beacons(), Counter()
    every: dict[tuple[str, str], int] = {}
    for e in window:
        key = (e["device"], e["domain"])
        if interval := tracker.observe(*key, e["ts"]):
            timed[key] += 1
            every[key] = interval
    if not timed:
        return None
    (device, domain), n = timed.most_common(1)[0]
    seconds = round(every[(device, domain)] / 1000)
    gap = plural(round(seconds / 60), "minute") if seconds >= 120 else plural(seconds, "second")
    return fact(
        f"The {device} checks in with {domain} on a timer",
        f"{plural(n, 'lookup')} about every {gap} in {hours}h",
        0.7,
    )


def spoken_text(facts: list[dict], hours: int) -> str:
    if not facts:
        span = "week" if hours == 168 else f"{hours} hours"
        return f"I haven't seen enough traffic in the last {span} to say what anyone could guess about you."
    lines = [f"{f['fact']}: {f['evidence']}." for f in facts[:3]]
    return "From what your devices sent, someone could guess this. " + " ".join(lines)
