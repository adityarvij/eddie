"""Decides allow / block / flag for one lookup (README "Decision order").

First match wins:
0. Quarantine (#98, quarantine.py): a denied device, or a new one while
   quarantine is on, is blocked except for the allowlist, before any rule.
1. Device rule, then the device's lockdown rule (pattern "*", #17), then global
   rule (the rules table). Under lockdown, an allowlisted domain is still
   allowed, so locking a device down can't cut it off from sign-in, updates or time.
2. Allowlist: essentials, so a rule or list can't brick a device.
3. Blocklist.
4. Keyword heuristic on the domain's labels.
5. Cached AI verdict: needed allows; tracker flags as a likely tracker;
   unsure flags. The AI never blocks on its own: on real traffic the local
   model called Google's static files, Apple's update servers and Safe
   Browsing trackers at 0.8-0.9 confidence. Blocking takes a list, the
   heuristic or a user rule.
6. Beacon: an unknown domain this device looks up on a regular timer
   (beacons.py) is flagged as likely telemetry, source heuristic. Still
   forwarded and queued for the classifier like any unknown.
7. Unknown: flag (the lookup is still forwarded). The flagged queries row in
   eddie.db is the classifier's queue (classifier.py); nothing else is kept.

This runs on the DNS hot path: no model calls, no network, no disk. Everything
it reads is in memory; the only state it writes is the bounded in-memory
lookup timing for unknown domains (beacons.Beacons). Writers (rules API #12, list loader #8, classifier #27, quarantine #98)
replace whole snapshots with set_rules / set_lists / set_verdicts / set_quarantined instead of
mutating them, so a lookup never sees a half-updated table.

Matching:
- Allowlist and blocklist entries match the domain and every subdomain of it.
- A rule pattern `a.com` matches only a.com; `*.a.com` matches a.com and every
  subdomain. When several rules match, the most specific wins (exact, then the
  longest suffix), and among equals the newest rule wins. The device rule `*`
  (block only) matches every domain and is the least specific device rule.
- A device rule matches the device's key (its MAC once devices.py knows it) or
  its client IP, so rules made before the MAC was known keep working.
"""
import re
import sqlite3
import time
from dataclasses import dataclass

from . import quarantine
from .beacons import Beacons
from .events import Action, Source

KEYWORDS = ("metrics", "telemetry", "analytics", "adsystem", "tracking", "beacon", "crashlytics")
# Too short to match inside a label (uploads, blog, login), so these must be a
# whole word: a label split on anything that isn't a letter.
WHOLE_WORDS = {"ads": "ads", "log": "log", "logs": "log"}
UNKNOWN = "unknown, waiting for classifier"
BEACON = "looked up every {} s, likely telemetry; waiting for classifier"
QUARANTINE = "new device, quarantined until approved"
DENIED = "device denied"

_WORD_SPLIT = re.compile(r"[^a-z]+")


@dataclass(frozen=True)
class Decision:
    action: Action
    source: Source
    reason: str


@dataclass(frozen=True)
class Rule:
    id: int
    pattern: str
    action: Action
    device_mac: str | None = None  # None for a global rule

    @classmethod
    def from_row(cls, row) -> "Rule":
        """A rules row: (id, scope, device_mac, pattern, action, ...)."""
        rule_id, scope, device_mac, pattern, action = row[:5]
        return cls(rule_id, pattern, action, device_mac if scope == "device" else None)


@dataclass(frozen=True)
class Verdict:
    label: str  # tracker / needed / unsure
    confidence: float
    reason: str = ""


def normalize(domain: str) -> str:
    return domain.strip().lower().rstrip(".")


def is_plumbing(domain: str) -> bool:
    """A lookup the network makes for itself, not an app reaching a company.

    Reverse lookups (*.arpa), service discovery (_ipp._tcp..., b._dns-sd._udp...),
    .local names and single-label names. They're still answered and logged, but
    left out of the live feed and the classifier's queue. Keep PLUMBING_SQL in step.
    """
    return ("." not in domain or domain.endswith((".arpa", ".local"))
            or any(label.startswith("_") for label in domain.split(".")))


# is_plumbing as a SQL condition on a domain column (true for plumbing).
PLUMBING_SQL = ("(domain NOT LIKE '%.%' OR domain LIKE '%.arpa' OR domain LIKE '%.local'"
                r" OR domain LIKE '\_%' ESCAPE '\' OR domain LIKE '%.\_%' ESCAPE '\')")


def suffixes(domain: str) -> list[str]:
    """The domain, then each parent: a.b.com, b.com, com."""
    labels = domain.split(".")
    return [".".join(labels[i:]) for i in range(len(labels))]


def matches(domain: str, entries) -> str | None:
    """The entry covering the domain (itself or a parent), or None."""
    return next((s for s in suffixes(domain) if s in entries), None)


def keyword(domain: str) -> str | None:
    """The tracker keyword found in the domain's labels (TLD excluded), or None."""
    for label in domain.split(".")[:-1]:
        for word in _WORD_SPLIT.split(label):
            if word in WHOLE_WORDS:
                return WHOLE_WORDS[word]
        for kw in KEYWORDS:
            if kw in label:
                return kw
    return None


class RuleIndex:
    """Rules keyed by the domain they match, most specific first on lookup."""

    def __init__(self, rules):
        self.exact: dict[str, list[Rule]] = {}
        self.wildcard: dict[str, list[Rule]] = {}
        self.everything: list[Rule] = []  # device lockdown rules
        for rule in sorted(rules, key=lambda r: r.id, reverse=True):  # newest first
            pattern = normalize(rule.pattern)
            if pattern == "*":
                if rule.device_mac is not None:  # a global "*" can't be stored; ignore one anyway
                    self.everything.append(rule)
            elif pattern.startswith("*."):
                self.wildcard.setdefault(pattern[2:], []).append(rule)
            elif pattern:
                self.exact.setdefault(pattern, []).append(rule)

    def find(self, domain: str, device: str, ip: str | None = None) -> Rule | None:
        candidates = self.exact.get(domain, []) + [
            r for s in suffixes(domain) for r in self.wildcard.get(s, [])] + self.everything
        return (next((r for r in candidates if r.device_mac is not None and r.device_mac in (device, ip)), None)
                or next((r for r in candidates if r.device_mac is None), None))


class Engine:
    def __init__(self):
        self.rules = RuleIndex([])
        self.allowlist: frozenset[str] = frozenset()
        self.blocklist: frozenset[str] = frozenset()
        self.verdicts: dict[str, Verdict] = {}
        self.quarantined: dict[str, str] = {}  # device MAC -> reason its lookups are blocked
        self.beacons = Beacons()

    def set_rules(self, rules) -> None:
        self.rules = RuleIndex(rules)

    def set_lists(self, allowlist, blocklist) -> None:
        self.allowlist = frozenset(map(normalize, allowlist))
        self.blocklist = frozenset(map(normalize, blocklist))

    def set_verdicts(self, verdicts: dict[str, Verdict]) -> None:
        self.verdicts = {normalize(d): v for d, v in verdicts.items()}

    def set_quarantined(self, held: dict[str, str]) -> None:
        self.quarantined = dict(held)

    def load_from_store(self, conn: sqlite3.Connection) -> None:
        """Replace rules, cached AI verdicts and quarantined devices with what's in eddie.db."""
        self.set_rules(Rule.from_row(r) for r in conn.execute(
            "SELECT id, scope, device_mac, pattern, action FROM rules"))
        self.set_verdicts({d: Verdict(label, confidence, reason or "") for d, label, confidence, reason
                           in conn.execute("SELECT domain, label, confidence, reason FROM ai_verdicts")})
        status = quarantine.statuses(conn)
        self.set_quarantined({mac: QUARANTINE if status[mac] == quarantine.PENDING else DENIED
                              for mac in quarantine.blocked(conn)})

    def evaluate(self, domain: str, device: str, ip: str | None = None, now_ms: int | None = None) -> Decision:
        domain = normalize(domain)
        if not domain:
            return Decision("allow", "default", "root query")
        if held := self.quarantined.get(device):
            if entry := matches(domain, self.allowlist):
                return Decision("allow", "allowlist", entry)
            return Decision("block", "quarantine", held)
        rule = self.rules.find(domain, device, ip)
        if rule and rule.pattern == "*" and (entry := matches(domain, self.allowlist)):
            return Decision("allow", "allowlist", entry)
        if rule:
            if rule.pattern == "*":
                return Decision("block", "rule", "device lockdown")
            scope = "device" if rule.device_mac else "global"
            return Decision(rule.action, "rule", f"{scope} rule {rule.pattern}")
        if entry := matches(domain, self.allowlist):
            return Decision("allow", "allowlist", entry)
        if entry := matches(domain, self.blocklist):
            return Decision("block", "blocklist", entry)
        if kw := keyword(domain):
            return Decision("block", "heuristic", f'keyword "{kw}"')
        if verdict := self.verdicts.get(domain):
            if verdict.label == "tracker":
                return Decision("flag", "ai", f"likely tracker: {verdict.reason}" if verdict.reason else "likely tracker")
            if verdict.label == "needed":
                return Decision("allow", "ai", verdict.reason)
            return Decision("flag", "ai", verdict.reason)
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if every := self.beacons.observe(device, domain, now_ms):
            return Decision("flag", "heuristic", BEACON.format(round(every / 1000)))
        return Decision("flag", "default", UNKNOWN)


engine = Engine()


def evaluate(domain: str, device: str, ip: str | None = None, now_ms: int | None = None) -> Decision:
    return engine.evaluate(domain, device, ip, now_ms)
