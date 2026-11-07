"""Decides allow / block / flag for one lookup. Stub until #10 lands.

The real order (README "Decision order") is user rules → allowlist →
blocklist → keyword heuristic → cached AI verdict → default allow + flag.
For now one hard-coded tracker is blocked so the sinkhole path can be tested.
This runs on the DNS hot path: no model calls, no network, no disk.
"""
from dataclasses import dataclass

from .events import Action, Source

STUB_BLOCKED = {"device-metrics-us.amazon.com"}


@dataclass(frozen=True)
class Decision:
    action: Action
    source: Source
    reason: str


def matches(domain: str, blocked: set[str]) -> bool:
    """Exact domain or any parent of it (blocking a.com also blocks x.a.com)."""
    labels = domain.split(".")
    return any(".".join(labels[i:]) in blocked for i in range(len(labels)))


def evaluate(domain: str, device: str) -> Decision:
    domain = domain.strip().lower().rstrip(".")
    if matches(domain, STUB_BLOCKED):
        return Decision("block", "blocklist", "stub blocklist")
    return Decision("allow", "default", "")
