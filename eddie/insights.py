"""Spoken traffic summary built only from real counts (served at /summary)."""
import time
from collections import defaultdict
from . import labels


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_text(events: list[dict], now_ms: int | None = None) -> str:
    now_ms = now_ms or int(time.time() * 1000)
    hour = [e for e in events if 0 <= now_ms - e["ts"] < 60 * 60 * 1000]
    if not hour:
        return "Quiet so far. Nobody's tried anything yet."
    blocked = [e for e in hour if e["action"] == "block"]
    devices = {e["device"] for e in hour}
    text = (f"In the last hour I saw {plural(len(hour), 'request')} from "
            f"{plural(len(devices), 'device')} and blocked {len(blocked)}.")
    if blocked:
        counts = defaultdict(int)
        for e in blocked:
            counts[(e["company"], e["category"])] += 1
        (company, category), n = max(counts.items(), key=lambda kv: kv[1])
        text += f" The biggest offender was {company} {category}, {plural(n, 'time')}."
    return text


def company_counts(events: list[dict], now_ms: int | None = None, limit: int = 10) -> list[dict]:
    """Counts are requested lookups, not proof of uploaded data or unique visits."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    companies = {}
    for event in events:
        if not 0 <= now_ms - event['ts'] < 3_600_000:
            continue
        company, category = labels.lookup(event['domain'])
        row = companies.setdefault(company, {'company': company, 'requests': 0, 'blocked': 0, 'tracking_requests': 0})
        row['requests'] += 1
        row['blocked'] += event['action'] == 'block'
        row['tracking_requests'] += category in labels.TRACKING
    return sorted(companies.values(), key=lambda row: (-row['requests'], row['company']))[:limit]
