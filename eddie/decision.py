"""Decides allow / block / flag for one lookup (README "Decision order").

First match wins:
1. Device rule, then global rule (the rules table).
2. Allowlist: essentials, so a rule or list can't brick a device.
3. Blocklist.
4. Keyword heuristic on the domain's labels.
5. Cached AI verdict: tracker at confidence >= 0.7 blocks, needed allows,
   anything else flags.
6. Unknown: flag (the lookup is still forwarded). The flagged queries row in
   eddie.db is the classifier's queue (classifier.py); nothing else is kept.

This runs on the DNS hot path: no model calls, no network, no disk. Everything
it reads is in memory. Writers (rules API #12, list loader #8, classifier #27)
replace whole snapshots with set_rules / set_lists / set_verdicts instead of
mutating them, so a lookup never sees a half-updated table.

Matching:
- Allowlist and blocklist entries match the domain and every subdomain of it.
- A rule pattern `a.com` matches only a.com; `*.a.com` matches a.com and every
  subdomain. When several rules match, the most specific wins (exact, then the
  longest suffix), and among equals the newest rule wins.
"""
import re
import sqlite3
from dataclasses import dataclass

from .events import Action, Source

KEYWORDS = ("metrics", "telemetry", "analytics", "adsystem", "tracking", "beacon", "crashlytics")
# Too short to match inside a label (uploads, blog, login), so these must be a
# whole word: a label split on anything that isn't a letter.
WHOLE_WORDS = {"ads": "ads", "log": "log", "logs": "log"}
TRACKER_CONFIDENCE = 0.7
UNKNOWN = "unknown, waiting for classifier"

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
        for rule in sorted(rules, key=lambda r: r.id, reverse=True):  # newest first
            pattern = normalize(rule.pattern)
            if pattern.startswith("*."):
                self.wildcard.setdefault(pattern[2:], []).append(rule)
            elif pattern:
                self.exact.setdefault(pattern, []).append(rule)

    def find(self, domain: str, device: str) -> Rule | None:
        candidates = self.exact.get(domain, []) + [
            r for s in suffixes(domain) for r in self.wildcard.get(s, [])]
        return (next((r for r in candidates if r.device_mac == device), None)
                or next((r for r in candidates if r.device_mac is None), None))


class Engine:
    def __init__(self):
        self.rules = RuleIndex([])
        self.allowlist: frozenset[str] = frozenset()
        self.blocklist: frozenset[str] = frozenset()
        self.verdicts: dict[str, Verdict] = {}

    def set_rules(self, rules) -> None:
        self.rules = RuleIndex(rules)

    def set_lists(self, allowlist, blocklist) -> None:
        self.allowlist = frozenset(map(normalize, allowlist))
        self.blocklist = frozenset(map(normalize, blocklist))

    def set_verdicts(self, verdicts: dict[str, Verdict]) -> None:
        self.verdicts = {normalize(d): v for d, v in verdicts.items()}

    def load_from_store(self, conn: sqlite3.Connection) -> None:
        """Replace rules and cached AI verdicts with what's in eddie.db."""
        self.set_rules(Rule.from_row(r) for r in conn.execute(
            "SELECT id, scope, device_mac, pattern, action FROM rules"))
        self.set_verdicts({d: Verdict(label, confidence, reason or "") for d, label, confidence, reason
                           in conn.execute("SELECT domain, label, confidence, reason FROM ai_verdicts")})

    def evaluate(self, domain: str, device: str) -> Decision:
        domain = normalize(domain)
        if not domain:
            return Decision("allow", "default", "root query")
        if rule := self.rules.find(domain, device):
            scope = "device" if rule.device_mac else "global"
            return Decision(rule.action, "rule", f"{scope} rule {rule.pattern}")
        if entry := matches(domain, self.allowlist):
            return Decision("allow", "allowlist", entry)
        if entry := matches(domain, self.blocklist):
            return Decision("block", "blocklist", entry)
        if kw := keyword(domain):
            return Decision("block", "heuristic", f'keyword "{kw}"')
        if verdict := self.verdicts.get(domain):
            if verdict.label == "tracker" and verdict.confidence >= TRACKER_CONFIDENCE:
                return Decision("block", "ai", verdict.reason)
            if verdict.label == "needed":
                return Decision("allow", "ai", verdict.reason)
            return Decision("flag", "ai", verdict.reason)
        return Decision("flag", "default", UNKNOWN)


engine = Engine()


def evaluate(domain: str, device: str) -> Decision:
    return engine.evaluate(domain, device)
