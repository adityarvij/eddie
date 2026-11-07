"""Spoken traffic summary built only from real counts (served at /summary)."""
import time
from collections import defaultdict


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def summary_text(events: list[dict], now_ms: int | None = None) -> str:
    now_ms = now_ms or int(time.time() * 1000)
    hour = [e for e in events if now_ms - e["ts"] < 60 * 60 * 1000]
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
