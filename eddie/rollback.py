"""Self-correcting blocks (#101): roll back a new block rule that looks like it broke a device.

The API watches each block rule POST /rules stores for WATCH_MS, using the
query events DNS sends to /ingest, so nothing here runs on the DNS hot path.
For each device the rule blocks it looks for two signs of breakage:

- Retry storm: STORM_HITS or more lookups blocked by the rule within
  STORM_MS. A healthy device caches a block answer for 60 s (dns_server.py)
  and asks again about once a minute; the feed sends a repeat of the same
  device, domain and action at most every 2 s, so 12 a minute is a device
  asking at least every 5 s.
- Traffic collapse: the device made COLLAPSE_BASELINE or more other lookups
  (anything not blocked, essentials included) in the BASELINE_MS before the
  rule, keeps asking for the blocked domain (COLLAPSE_HITS or more blocked
  lookups) and, COLLAPSE_AFTER_MS or more after its first blocked lookup, its
  other lookups have fallen to at most COLLAPSE_RATIO of its earlier rate. A
  device that was switched off or fell asleep stops asking for the blocked
  domain too, so silence alone never counts.

Either sign is a guess, not proof the device broke: an app may retry a
blocked tracker forever and work fine, and a household may simply stop
using a device. The reason text says so.

Never watched: allow and spoof rules, lockdown ("*"), pinned rules, and rules
stored before this API process started (watches are memory only).

Keeps only what the signs need, in memory: per device, the times (no domains)
of its unblocked lookups over the last BASELINE_MS + WATCH_MS, and per watched
rule the times of the lookups it blocked.
"""
from collections import deque
from dataclasses import dataclass, field

WATCH_MS = 10 * 60_000  # how long after it's added a block rule can be rolled back
STORM_HITS = 12
STORM_MS = 60_000
BASELINE_MS = 10 * 60_000  # the device's normal rate: other lookups in this long before the rule
COLLAPSE_BASELINE = 20  # fewer other lookups than this before the rule: too quiet to judge a drop
COLLAPSE_HITS = 3
COLLAPSE_AFTER_MS = 3 * 60_000
COLLAPSE_RATIO = 0.1
HISTORY_MS = BASELINE_MS + WATCH_MS
MAX_HISTORY = 5000  # other-lookup times kept per device
MAX_WATCHES = 200

GUESS = "That's a guess, not proof it broke."


@dataclass
class Watch:
    rule: dict  # the stored rules row
    hits: dict[str, deque] = field(default_factory=dict)  # device key -> times of lookups this rule blocked


@dataclass(frozen=True)
class Rollback:
    rule: dict
    device_mac: str  # the device that looked broken
    device: str
    sign: str  # retry_storm | traffic_collapse
    reason: str


def watchable(rule: dict) -> bool:
    return rule["action"] == "block" and rule["pattern"] != "*" and not rule.get("pinned")


def blocked_by(rule: dict, event: dict) -> bool:
    """Whether the rule is what blocked this lookup: its reason names it (decision.py) and the device matches."""
    if event["action"] != "block" or event["source"] != "rule":
        return False
    scope = "device" if rule["device_mac"] else "global"
    if event.get("reason") != f"{scope} rule {rule['pattern']}":
        return False
    key = event["device_mac"]
    return rule["device_mac"] is None or rule["device_mac"] in (key, key.removeprefix("ip:"))


def per_minute(count: int, ms: int) -> str:
    rate = count * 60_000 / ms
    return f"{rate:.0f}" if rate >= 1 or rate == 0 else "under 1"


class Watcher:
    def __init__(self):
        self.watches: dict[int, Watch] = {}
        self.history: dict[str, deque] = {}  # device key -> times of its unblocked lookups

    def watch(self, rule: dict) -> None:
        if not watchable(rule):
            return
        if len(self.watches) >= MAX_WATCHES:
            del self.watches[next(iter(self.watches))]
        self.watches[rule["id"]] = Watch(rule)

    def forget(self, rule_id: int) -> None:
        self.watches.pop(rule_id, None)

    def observe(self, event: dict) -> list[Rollback]:
        """Take one query event; return the rules to roll back because of it (each once)."""
        now, key = event["ts"], event["device_mac"]
        for rule_id in [i for i, w in self.watches.items() if now - w.rule["ts"] > WATCH_MS]:
            del self.watches[rule_id]
        if event["action"] != "block":
            times = self.history.setdefault(key, deque(maxlen=MAX_HISTORY))
            times.append(now)
        for dev in list(self.history):
            times = self.history[dev]
            while times and times[0] < now - HISTORY_MS:
                times.popleft()
            if not times:
                del self.history[dev]
        out = []
        for watch in sorted(self.watches.values(), key=lambda w: w.rule["id"], reverse=True):
            if blocked_by(watch.rule, event):
                hits = watch.hits.setdefault(key, deque(maxlen=MAX_HISTORY))
                hits.append(now)
                if reason := self.storm(watch, event, hits) or self.collapse(watch, event, hits):
                    sign, text = reason
                    out.append(Rollback(watch.rule, key, event["device"], sign, text))
                    del self.watches[watch.rule["id"]]
                break  # one rule answered this lookup: the newest watched one that names it
        return out

    def storm(self, watch: Watch, event: dict, hits: deque) -> tuple[str, str] | None:
        recent = sum(1 for t in hits if t > event["ts"] - STORM_MS)
        if recent < STORM_HITS:
            return None
        return "retry_storm", (
            f"I removed the block on {watch.rule['pattern']} for {event['device']}: it asked for "
            f"{event['domain']} {recent} times in a minute after the block, which can mean it stopped "
            f"working. {GUESS} Block it again as a pinned rule to keep it.")

    def collapse(self, watch: Watch, event: dict, hits: deque) -> tuple[str, str] | None:
        now, start = event["ts"], watch.rule["ts"]
        first = hits[0]
        times = self.history.get(event["device_mac"], ())
        before = sum(1 for t in times if start - BASELINE_MS <= t < start)
        if len(hits) < COLLAPSE_HITS or before < COLLAPSE_BASELINE or now - first < COLLAPSE_AFTER_MS:
            return None
        after = sum(1 for t in times if first < t <= now)
        if after > COLLAPSE_RATIO * before * (now - first) / BASELINE_MS:
            return None
        return "traffic_collapse", (
            f"I removed the block on {watch.rule['pattern']} for {event['device']}: since the block it "
            f"kept asking for {event['domain']} while its other lookups fell from about "
            f"{per_minute(before, BASELINE_MS)} to {per_minute(after, now - first)} a minute, which can "
            f"mean it stopped working. {GUESS} Block it again as a pinned rule to keep it.")
