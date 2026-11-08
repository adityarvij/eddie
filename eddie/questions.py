"""Ask before blocking (#79): AI tracker verdicts become questions for the household.

The classifier only flags (decision.py); Eddie never blocks on a model's word.
A `tracker` verdict is offered here, and the person answers Block, Allow or
Ignore on the dashboard or by voice. Only Block or Allow writes a rule (api.py).

Rate limit: one open question at a time, at most one new one every ASK_EVERY
seconds. Trackers that arrive meanwhile wait and are grouped by device ("3 new
trackers on Fire Stick"), up to MAX_DOMAINS per question. A domain is offered
once per API run, so it is never asked about twice.

Memory only: a question holds its domains, each verdict's reason and the
device's display name. Nothing goes to eddie.db; it all goes when the API restarts.
"""
import itertools
import time

ASK_EVERY = 180  # seconds between questions
MAX_DOMAINS = 5  # per question; the rest wait for the next one
MAX_OFFERED = 5000  # domains remembered as already offered, oldest forgotten first


def prompt(device: str | None, domains: list[str]) -> str:
    where = f"on {device}" if device else "on your network"
    if len(domains) == 1:
        return f"A new likely tracker {where}: {domains[0]}. Block it?"
    listed = f"{', '.join(domains[:-1])} and {domains[-1]}"
    return f"{len(domains)} new likely trackers {where}: {listed}. Block them?"


class Questions:
    def __init__(self, every: float = ASK_EVERY, clock=time.monotonic):
        self.every = every
        self.clock = clock
        self.offered: dict[str, None] = {}  # insertion-ordered set
        self.waiting: dict[str | None, dict[str, str]] = {}  # device -> {domain: reason}
        self.open: dict[int, dict] = {}
        self.asked_at: float | None = None
        self.ids = itertools.count(1)

    def offer(self, domain: str, reason: str, device: str | None) -> bool:
        """Queue a tracker verdict. False if this domain was offered before."""
        if domain in self.offered:
            return False
        if len(self.offered) >= MAX_OFFERED:
            self.offered.pop(next(iter(self.offered)))
        self.offered[domain] = None
        self.waiting.setdefault(device, {})[domain] = reason
        return True

    def release(self, now_ms: int | None = None) -> dict | None:
        """The next question when one is due, grouping the busiest device's waiting trackers; else None."""
        now = self.clock()
        if self.open or not self.waiting or (self.asked_at is not None and now - self.asked_at < self.every):
            return None
        device = max(self.waiting, key=lambda d: len(self.waiting[d]))
        pending = self.waiting[device]
        domains = list(pending)[:MAX_DOMAINS]
        question = {"id": next(self.ids), "device": device,
                    "domains": [{"domain": d, "reason": pending.pop(d)} for d in domains],
                    "prompt": prompt(device, domains),
                    "ts": int(time.time() * 1000) if now_ms is None else now_ms}
        if not pending:
            del self.waiting[device]
        self.open[question["id"]] = question
        self.asked_at = now
        return question

    def take(self, question_id: int) -> dict | None:
        """Remove and return an open question, so it is answered once."""
        return self.open.pop(question_id, None)

    def pending(self) -> list[dict]:
        return list(self.open.values())
