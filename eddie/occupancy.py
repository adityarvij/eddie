"""Burglar's-eye report (#34): when could someone tell the house was empty? (served at /burglars-eye).

Built only from the event log, like inferences.py; nothing here is invented.
Every local clock hour with at least one lookup is "busy" when some device
streamed in it (labels.HUMAN) and "quiet" otherwise. Hours with no lookups at
all are left out: Eddie may have been off, so they prove nothing. DNS can't
tell empty from asleep, so only waking hours (6 AM to midnight) count toward
when the house looked empty.

- Range: the report uses the hours asked for when the data reaches back that
  far ("From this week's data"). When it doesn't, it says the report is from
  recent data and how much ("From recent data (2 days, not the full week)").
  With too little data it says so instead of concluding anything.
- When empty: hours quiet on at least 80% of the days Eddie saw them, on at
  least 2 days, split into weekdays and weekends when each has 2 days, merged
  into stretches of 2 hours or more. A day counts once Eddie saw at least
  half its waking hours; with fewer than 2 such days there is no pattern, so
  it lists the quiet stretches it saw and says they aren't one.
- Occupancy leak score per device, 0-100: |a - b| / (a + b) of the device's
  lookups per busy hour (a) and per quiet hour (b). Steady telemetry scores
  near 0; a device that only talks while someone is home (or only while
  nobody is) scores near 100. Under MIN_LOOKUPS lookups gets no score.
- Security devices (any lookup in a labels.SECURITY category) are flagged
  never_block: Eddie's advice is to keep them online whatever they leak. This
  is a flag in the report only; nothing here enforces it (#46).
"""
import time
from collections import Counter, defaultdict
from datetime import datetime

from .inferences import HOUR_MS, fact
from .insights import plural
from .labels import HUMAN, SECURITY

WAKING = range(6, 24)
MIN_OBSERVED_HOURS = 6  # hours with any lookup before saying anything
MIN_DAYS = 2  # days of a kind before calling an hour usually quiet
FULL_DAY = len(WAKING) // 2  # waking hours seen before a day counts toward a pattern
USUAL = 0.8  # share of those days the hour has to be quiet
MIN_STRETCH_HOURS = 2
MIN_LOOKUPS = 10  # per device, for a leak score
WEEK = 168


def hour_of(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000).replace(minute=0, second=0, microsecond=0)


def hour_name(h: int) -> str:
    h %= 24
    if h == 0:
        return "midnight"
    if h == 12:
        return "noon"
    return f"{h % 12} {'AM' if h < 12 else 'PM'}"


def span_words(hours: int) -> str:
    if hours == WEEK:
        return "this week"
    if hours % 24 == 0:
        return "the last day" if hours == 24 else f"the last {hours // 24} days"
    return f"the last {plural(hours, 'hour')}"


def span_in(hours: int) -> str:
    return span_words(hours) if hours == WEEK else f"in {span_words(hours)}"


def amount(hours: int) -> str:
    return plural(hours // 24, "day") if hours >= 48 else plural(hours, "hour")


def hour_grid(window: list[dict]) -> dict[datetime, bool]:
    """Local hour -> True if someone streamed in it. Hours with no lookups are absent."""
    grid: dict[datetime, bool] = {}
    for e in window:
        h = hour_of(e["ts"])
        grid[h] = grid.get(h, False) or e["category"] in HUMAN
    return grid


def runs(hours: list[int]) -> list[tuple[int, int]]:
    """Sorted hours -> (start, end) stretches of consecutive hours, end exclusive, MIN_STRETCH_HOURS or longer."""
    out: list[tuple[int, int]] = []
    for h in hours:
        if out and out[-1][1] == h:
            out[-1] = (out[-1][0], h + 1)
        else:
            out.append((h, h + 1))
    return [r for r in out if r[1] - r[0] >= MIN_STRETCH_HOURS]


def from_to(stretches: list[tuple[int, int]]) -> str:
    return " and ".join(f"from {hour_name(a)} to {hour_name(b)}" for a, b in stretches)


def usual_quiet(grid: dict[datetime, bool], kind: str, days: set) -> dict | None:
    """Waking stretches quiet on at least USUAL of the given days Eddie saw them, as one fact."""
    seen, quiet = Counter(), Counter()
    for h, busy in grid.items():
        if h.date() in days and h.hour in WAKING:
            seen[h.hour] += 1
            quiet[h.hour] += not busy
    usual = [h for h in WAKING if seen[h] >= MIN_DAYS and quiet[h] >= USUAL * seen[h]]
    stretches = runs(usual)
    if not stretches:
        return None
    hours = [h for start, end in stretches for h in range(start, end)]
    q, s = min(quiet[h] for h in hours), max(seen[h] for h in hours)
    when = f"on {kind} {from_to(stretches)}"
    noun = {"weekends": "weekend days", "most days": "days"}.get(kind, kind)
    return {"when": when, "pattern": True, **fact(
        f"The house looks empty {when}",
        f"no streaming in those hours on {q} of the {s} {noun} Eddie saw",
        min(0.8, 0.4 + 0.05 * s), "Safety")}


def one_day_quiet(grid: dict[datetime, bool]) -> list[dict]:
    """Quiet waking stretches from a single day: what happened, not a pattern."""
    by_day = defaultdict(list)
    for h, busy in grid.items():
        if h.hour in WAKING and not busy:
            by_day[h.date()].append(h.hour)
    out = []
    for day, hours in sorted(by_day.items()):
        if stretches := runs(sorted(hours)):
            when = f"on {day.strftime('%a')} {from_to(stretches)}"
            quiet = sum(b - a for a, b in stretches)
            out.append({"when": when, "pattern": False, **fact(
                f"The house looked empty {when}",
                f"no streaming in those {plural(quiet, 'hour')}; under {MIN_DAYS} days of data, so not a pattern",
                0.3, "Safety")})
    return out


def empty_times(grid: dict[datetime, bool]) -> list[dict]:
    days = {d for d, n in Counter(h.date() for h in grid if h.hour in WAKING).items() if n >= FULL_DAY}
    weekdays = {d for d in days if d.weekday() < 5}
    weekends = days - weekdays
    if len(days) < MIN_DAYS:
        return one_day_quiet(grid)
    groups = [(kind, group) for kind, group in (("weekdays", weekdays), ("weekends", weekends))
              if len(group) >= MIN_DAYS]
    if not groups:
        groups = [("most days", days)]
    return [f for kind, group in groups if (f := usual_quiet(grid, kind, group))]


def device_scores(window: list[dict], grid: dict[datetime, bool]) -> list[dict]:
    """Devices ranked by occupancy leak score, highest first; unscored devices last."""
    busy_hours = sum(grid.values())
    quiet_hours = len(grid) - busy_hours
    per = defaultdict(lambda: {"busy": 0, "quiet": 0, "blocked": 0, "security": False})
    for e in window:
        d = per[e["device"]]
        d["busy" if grid[hour_of(e["ts"])] else "quiet"] += 1
        d["blocked"] += e["action"] == "block"
        d["security"] |= e["category"] in SECURITY
    rows = []
    for device, d in per.items():
        lookups = d["busy"] + d["quiet"]
        row = {"device": device, "leak_score": None, "lookups": lookups, "blocked": d["blocked"],
               "security": d["security"], "never_block": d["security"]}
        if lookups >= MIN_LOOKUPS and busy_hours and quiet_hours:
            a, b = round(d["busy"] / busy_hours, 1), round(d["quiet"] / quiet_hours, 1)
            score = round(100 * abs(a - b) / (a + b)) if a + b else 0
            row.update(leak_score=score, per_busy_hour=a, per_quiet_hour=b, said=(
                f"The {device} has an occupancy leak score of {score} out of 100: {a} lookups an hour "
                f"while someone was streaming vs {b} when nobody was"))
        else:
            row["said"] = f"Not enough traffic from the {device} to score it ({plural(lookups, 'lookup')})"
        rows.append(row)
    return sorted(rows, key=lambda r: (r["leak_score"] is None, -(r["leak_score"] or 0), r["device"]))


def report(events: list[dict], hours: int = WEEK, now_ms: int | None = None) -> dict:
    """The burglar's-eye report for the last `hours`, or as much of it as the data covers."""
    now_ms = now_ms or int(time.time() * 1000)
    window = sorted((e for e in events if 0 <= now_ms - e["ts"] < hours * HOUR_MS), key=lambda e: e["ts"])
    covered = -(-(now_ms - window[0]["ts"]) // HOUR_MS) if window else 0  # hours, rounded up
    full = covered >= hours - max(1, hours // 7)  # a day short of a week still counts as the week
    span = span_words(hours)
    if full:
        lead = "From this week's data" if hours == WEEK else f"From {span}"
    else:
        lead = f"From recent data ({amount(covered)}, not {'the full week' if hours == WEEK else span})"
    out = {"hours": hours, "covered_hours": covered, "full_range": full,
           "enough": False, "empty_times": [], "devices": []}

    grid = hour_grid(window)
    if not window:
        return {**out, "text": f"I haven't seen any traffic {span_in(hours)}, so I can't tell when the house was empty."}
    if len(grid) < MIN_OBSERVED_HOURS:
        return {**out, "text": f"I've only seen {plural(len(grid), 'hour')} of traffic {span_in(hours)}, "
                               "not enough to tell when the house was empty."}
    if not any(grid.values()):
        return {**out, "text": f"{lead}, nobody streamed anything, so I can't tell home from away."}

    times = [] if all(grid.values()) else empty_times(grid)
    devices = device_scores(window, grid)
    out.update(enough=True, empty_times=times, devices=devices)
    if times:
        text = f"{lead}, someone could tell the house was empty {' and '.join(t['when'] for t in times)}."
        if not times[0]["pattern"]:
            text += f" That's under {MIN_DAYS} days of data, so it's not a pattern yet."
    else:
        text = f"{lead}, I didn't find {MIN_STRETCH_HOURS} or more waking hours in a row that looked empty."
    scored = [d for d in devices if d["leak_score"] is not None]
    if scored:
        text += f" {scored[0]['said']}."
        if len(scored) > 1:
            text += f" Next is the {scored[1]['device']} at {scored[1]['leak_score']}."
    else:
        text += " No device sent enough traffic for a leak score."
    for d in [d for d in devices if d["security"]][:2]:
        text += f" The {d['device']} is a security device, so keep it online: never block it."
    return {**out, "text": text}
