""""What they know about you": household inferences from real traffic (served at /what-they-know).

Scaffold of README "Intelligence features → Priority 1". Code computes facts
from the event log; nothing here is invented. Each fact is
{fact, evidence, confidence} so a local model can later phrase them.

Built: company exposure, active hours, late-night use, idle telemetry,
beacons (a domain looked up on a timer), longest quiet stretch. Not built yet: sessions by category and weekday,
camera/doorbell occupancy signals, the Ollama phrasing step and the
number check that drops any model line whose numbers aren't in the facts.
"""
import time
from collections import Counter
from datetime import datetime

from .beacons import Beacons
from .insights import plural
from .labels import HUMAN, TRACKING

HOUR_MS = 60 * 60 * 1000
IDLE_MS = 5 * 60 * 1000  # no human traffic for this long = nobody is using the device
LATE_HOURS = range(0, 5)  # midnight to 5 AM local


def clock(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%-I:%M %p")


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
    if human:
        facts.append(fact(
            "Someone was using a screen between these times",
            f"streaming traffic from {clock(human[0]['ts'])} to {clock(human[-1]['ts'])}",
            0.7,
        ))
        late = [e for e in human if datetime.fromtimestamp(e["ts"] / 1000).hour in LATE_HOURS]
        if late:
            facts.append(fact(
                "Someone is up watching after midnight",
                f"{plural(len(late), 'streaming request')} between midnight and 5 AM",
                0.6,
            ))
        gaps = [(b["ts"] - a["ts"], a["ts"]) for a, b in zip(human, human[1:])]
        if gaps:
            gap, start = max(gaps)
            if gap >= HOUR_MS:
                facts.append(fact(
                    "The house may have been empty or asleep for a stretch",
                    f"no streaming for {gap // HOUR_MS}h starting {clock(start)}",
                    0.5,
                ))

    # Tracking while nobody is watching: the device reports on its own.
    human_ts = [e["ts"] for e in human]
    idle = [e for e in tracking if not any(0 <= e["ts"] - t < IDLE_MS for t in human_ts)]
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
    # TODO: replace with the local model prompt from the README, then validate
    # that every number in its output appears in `facts` before speaking it.
    if not facts:
        return f"I haven't seen enough traffic in the last {hours} hours to say what anyone could guess about you."
    lines = [f"{f['fact']}: {f['evidence']}." for f in facts[:3]]
    return "From what your devices sent, someone could guess this. " + " ".join(lines)
