"""Labels unknown domains with the local model, off the DNS path.

The queue is eddie.db itself: every lookup the decision engine couldn't place
is already a queries row with source "default". This worker picks recent ones
that have no verdict yet, asks Ollama, and writes the verdict to ai_verdicts.
The DNS server reloads verdicts within about a second (dns_server.StoreWatcher),
so it never waits on the model. Nothing new about the household is stored.

Privacy (docs/PRIVACY.md):
- The model gets only the domain, the device's label (never its MAC or IP),
  lookups in the last 10 minutes and whether the device was idle.
- EDDIE_OLLAMA_URL must be localhost or a private LAN IP literal. Public hosts
  and hostnames are refused, redirects aren't followed and proxy settings are
  ignored, so lookups can't leave the house through the model.
- Domains, model output and verdicts are never written to the log.
- Malformed model output is rejected, not stored.
- Verdicts are pruned with the query log after EDDIE_RETENTION_DAYS.

Run:  uv run python -m eddie.classifier    (Ollama must be running)
"""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

from . import labels, store
from .decision import PLUMBING_SQL, Verdict

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2:3b"
LABELS = {"tracker", "needed", "unsure"}
MAX_REASON_WORDS = 14  # README contract: "<15 words"
MAX_REASON_CHARS = 120
LOOKBACK_MS = 3_600_000  # only unknowns looked up in the last hour are queued
COUNT_WINDOW_MS = 600_000  # "query count in the last 10 min"
IDLE_WINDOW_MS = 300_000  # "no allowed media traffic in 5 min"
BATCH = 10  # domains per synchronous compatibility pass
POLL_EVERY = 0.25  # seconds between passes
UNREACHABLE_WAIT = 10.0  # seconds to wait when Ollama is down
REJECT_BACKOFF = 300.0  # seconds before retrying a domain whose answer was rejected
MODEL_TIMEOUT = 7.0
MAX_IN_FLIGHT = 2
DOMAIN = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")

PROMPT = """You label one internet domain looked up by a device on a home network.
Judge mainly by what the domain is: who runs it and what it is for.
- "tracker": its main purpose is ads, analytics, telemetry, crash reports or usage data collection, and the device works without it.
- "needed": content, video or app APIs, CDNs, sign-in, payments, software updates, time sync or certificate checks. Blocking it breaks something.
- "unsure": you don't recognise the domain or it could be either.
queries_10min and idle (the device had no media traffic in the last 5 minutes; null means unknown) are weak hints only. Never call a domain a tracker just because it is looked up often or while idle.
confidence is how sure you are of the label, from 0 to 1. Use below 0.7 unless you recognise the domain.
Reply with JSON only: {"label": "tracker" | "needed" | "unsure", "confidence": <number>, "reason": "<under 15 words, about what the domain is>"}"""

log = logging.getLogger("eddie.classifier")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx becomes an error instead of a request somewhere else


# No ProxyHandler entries: HTTP(S)_PROXY in the environment is ignored.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def check_local(url: str) -> None:
    """Raise ValueError unless url is http(s) to localhost or a private IP literal."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{url} is not an http(s) URL")
    if parts.hostname == "localhost":
        return
    try:
        addr = ipaddress.ip_address(parts.hostname)
    except ValueError:
        raise ValueError(f"{parts.hostname} is a hostname; use localhost or a private IP so DNS can't send it elsewhere")
    if not (addr.is_loopback or addr.is_private) or addr.is_unspecified:
        raise ValueError(f"{addr} is not a loopback or private address")


def parse_verdict(text) -> Verdict | None:
    """The model's answer if it is exactly the contract's JSON, else None."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"label", "confidence", "reason"}:
        return None
    label, confidence, reason = data["label"], data["confidence"], data["reason"]
    if not isinstance(label, str) or label not in LABELS:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        return None
    if not isinstance(reason, str):
        return None
    reason = " ".join(reason.split())
    if not reason or len(reason.split()) > MAX_REASON_WORDS or len(reason) > MAX_REASON_CHARS \
            or not reason.isprintable():
        return None
    return Verdict(label, float(confidence), reason)


def candidates(conn: sqlite3.Connection, now_ms: int, limit: int) -> list[str]:
    """Recently looked-up unknown domains with no verdict yet, oldest first.

    Network plumbing (decision.is_plumbing) is skipped: it isn't a company to judge.
    """
    return [d for (d,) in conn.execute(
        "SELECT domain FROM queries WHERE ts >= ? AND source = 'default'"
        f" AND NOT {PLUMBING_SQL}"
        " AND domain NOT IN (SELECT domain FROM ai_verdicts)"
        " GROUP BY domain ORDER BY MIN(ts) ASC, domain ASC LIMIT ?", (now_ms - LOOKBACK_MS, limit))]


def facts(conn: sqlite3.Connection, domain: str, now_ms: int) -> dict:
    """Everything the model is told. Nothing else may be added here."""
    device = conn.execute(
        "SELECT device_mac FROM queries WHERE domain = ? AND ts >= ?"
        " GROUP BY device_mac ORDER BY COUNT(*) DESC LIMIT 1", (domain, now_ms - LOOKBACK_MS)).fetchone()
    device = device[0] if device else None
    label = conn.execute("SELECT label FROM devices WHERE mac = ?", (device,)).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM queries WHERE domain = ? AND ts >= ?",
                         (domain, now_ms - COUNT_WINDOW_MS)).fetchone()[0]
    return {"domain": domain, "device": label[0] if label else None, "queries_10min": count,
            "idle": idle(conn, device, now_ms)}


def idle(conn: sqlite3.Connection, device: str | None, now_ms: int) -> bool | None:
    """No allowed, labelled media traffic for five minutes; unknown if never seen."""
    if device is None:
        return None
    media = [ts for domain, ts in conn.execute(
        "SELECT domain, MAX(ts) FROM queries WHERE device_mac = ? AND action = 'allow'"
        " AND ts BETWEEN ? AND ? GROUP BY domain", (device, now_ms-LOOKBACK_MS, now_ms))
        if labels.lookup(domain)[1] in labels.HUMAN]
    if not media:
        return None
    return max(media) < now_ms-IDLE_WINDOW_MS


def ask_ollama(question: dict, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
               timeout: float = MODEL_TIMEOUT) -> str:
    """The model's raw reply text. Raises OSError if Ollama can't be reached."""
    check_local(url)
    body = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0, "num_predict": 160},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": json.dumps(question)}]}
    request = urllib.request.Request(url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read()
    try:
        return json.loads(raw)["message"]["content"]
    except (ValueError, KeyError, TypeError):
        return ""  # rejected by parse_verdict


class Classifier:
    def __init__(self, conn: sqlite3.Connection, ask):
        self.conn = conn
        self.ask = ask
        self.retry_at: dict[str, float] = {}  # domain -> monotonic time it may be retried

    def run_once(self, limit: int = BATCH) -> int:
        """Classify up to limit queued domains. Returns verdicts stored.

        Raises OSError if the model can't be reached, so the caller can wait.
        """
        now_ms = int(time.time() * 1000)
        now = time.monotonic()
        self.retry_at = {d: t for d, t in self.retry_at.items() if t > now}
        due = [d for d in candidates(self.conn, now_ms, limit + len(self.retry_at)) if d not in self.retry_at]
        stored = 0
        for domain in due[:limit]:
            if not DOMAIN.fullmatch(domain) or len(domain) > 253:
                self.retry_at[domain] = now + REJECT_BACKOFF  # never sent to the model
                continue
            verdict = parse_verdict(self.ask(facts(self.conn, domain, now_ms)))
            if verdict is None:
                log.warning("rejected malformed model output")
                self.retry_at[domain] = now + REJECT_BACKOFF
                continue
            with self.conn:
                self.conn.execute("INSERT OR REPLACE INTO ai_verdicts VALUES (?, ?, ?, ?, ?)",
                                  (domain, verdict.label, verdict.confidence, verdict.reason,
                                   int(time.time() * 1000)))
            stored += 1
        return stored


def publish_verdict(event: dict) -> None:
    """Publish metadata to the local API, never to an external service."""
    base = os.environ.get('EDDIE_API_URL', 'http://127.0.0.1:8000').rstrip('/')
    check_local(base)
    req = urllib.request.Request(base + '/ai/verdict', data=json.dumps(event).encode(),
                                 headers={'Content-Type': 'application/json'})
    with _OPENER.open(req, timeout=1) as response:
        if json.load(response).get('ok') is not True:
            raise OSError('Verdict event was not acknowledged')


class AsyncClassifier(Classifier):
    """Bounded background HTTP inference; SQLite remains on the owner thread.

    tick never waits for model futures. A full worker pool leaves the rest
    in the existing query log, with FIFO scheduling and no duplicate jobs.
    """
    def __init__(self, conn, ask, publish=publish_verdict, workers=MAX_IN_FLIGHT):
        super().__init__(conn, ask)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='eddie-classifier')
        self.publish = publish
        self.workers = workers
        self.pending = {}
        self.outbox = {}
        self.event_retry_at = 0

    def tick(self):
        now = time.monotonic()
        now_ms = int(time.time()*1000)
        stored = 0
        for domain, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[domain]
            try:
                verdict = parse_verdict(future.result())
            except Exception:
                self.retry_at[domain] = now + UNREACHABLE_WAIT
                log.warning('model request failed; retry scheduled')
                continue
            if verdict is None:
                self.retry_at[domain] = now + REJECT_BACKOFF
                log.warning('rejected malformed model output')
                continue
            ts = int(time.time()*1000)
            with self.conn:
                self.conn.execute('INSERT OR REPLACE INTO ai_verdicts VALUES (?, ?, ?, ?, ?)',
                                  (domain, verdict.label, verdict.confidence, verdict.reason, ts))
            # Existing cache remains authoritative if event delivery is down.
            if len(self.outbox) >= 1000:
                self.outbox.pop(next(iter(self.outbox)))
            self.outbox[domain] = {'domain': domain, 'label': verdict.label,
                                  'confidence': verdict.confidence, 'reason': verdict.reason, 'ts': ts}
            stored += 1
        if now >= self.event_retry_at:
            for domain in list(self.outbox)[:10]:
                try:
                    self.publish(self.outbox[domain])
                except Exception:
                    self.event_retry_at = now + 2
                    log.warning('verdict event unavailable; retry scheduled')
                    break
                del self.outbox[domain]
        self.retry_at = {d:t for d,t in self.retry_at.items() if t > now}
        slots = self.workers-len(self.pending)
        if slots:
            due = candidates(self.conn, now_ms, slots+len(self.pending)+len(self.retry_at))
            for domain in due:
                if domain in self.pending or domain in self.retry_at:
                    continue
                if not DOMAIN.fullmatch(domain) or len(domain)>253:
                    self.retry_at[domain] = now+REJECT_BACKOFF
                    continue
                question = facts(self.conn, domain, now_ms)
                self.pending[domain] = self.pool.submit(self.ask, question)
                if len(self.pending) >= self.workers:
                    break
        return stored

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    path = os.environ.get("EDDIE_DB", "eddie.db")
    if path == "off":
        raise SystemExit("The classifier's queue is the query log in eddie.db; with EDDIE_DB=off there is nothing to classify.")
    url = os.environ.get("EDDIE_OLLAMA_URL", DEFAULT_URL)
    try:
        check_local(url)
    except ValueError as exc:
        raise SystemExit(f"EDDIE_OLLAMA_URL refused: {exc}.")
    model = os.environ.get("EDDIE_CLASSIFIER_MODEL", DEFAULT_MODEL)
    worker = AsyncClassifier(store.connect(path), lambda question: ask_ollama(question, url, model))
    log.info("classifying unknown domains in %s with %s at %s", path, model, url)
    try:
        while True:
            try:
                stored = worker.tick()
                if stored:
                    log.info("stored %d verdicts", stored)
                time.sleep(POLL_EVERY)
            except OSError as exc:
                log.warning("Ollama unreachable (%s), retrying in %gs", type(exc).__name__, UNREACHABLE_WAIT)
                time.sleep(UNREACHABLE_WAIT)
            except sqlite3.Error as exc:
                log.warning("eddie.db error (%s)", exc)
                time.sleep(POLL_EVERY)
    except KeyboardInterrupt:
        pass
    finally:
        worker.close()
        worker.conn.close()


if __name__ == "__main__":
    main()
