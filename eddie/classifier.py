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
import ipaddress
import json
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request

from . import store
from .decision import Verdict

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2:3b"
LABELS = {"tracker", "needed", "unsure"}
MAX_REASON_WORDS = 14  # README contract: "<15 words"
MAX_REASON_CHARS = 120
LOOKBACK_MS = 3_600_000  # only unknowns looked up in the last hour are queued
COUNT_WINDOW_MS = 600_000  # "query count in the last 10 min"
IDLE_WINDOW_MS = 300_000  # "no allowed media traffic in 5 min"
BATCH = 10  # domains per pass
POLL_EVERY = 2.0  # seconds between passes
UNREACHABLE_WAIT = 10.0  # seconds to wait when Ollama is down
REJECT_BACKOFF = 300.0  # seconds before retrying a domain whose answer was rejected
MODEL_TIMEOUT = 30.0
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
    if label not in LABELS:
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
    """Recently looked-up unknown domains with no verdict yet, most recent first."""
    return [d for (d,) in conn.execute(
        "SELECT domain FROM queries WHERE ts >= ? AND source = 'default'"
        " AND domain NOT IN (SELECT domain FROM ai_verdicts)"
        " GROUP BY domain ORDER BY MAX(ts) DESC LIMIT ?", (now_ms - LOOKBACK_MS, limit))]


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
    """No allowlisted (media and essentials) traffic in 5 min; None if never seen any."""
    def allowed_since(ms):
        return conn.execute("SELECT 1 FROM queries WHERE device_mac = ? AND source = 'allowlist'"
                            " AND ts >= ? LIMIT 1", (device, now_ms - ms)).fetchone() is not None
    if device is None or not allowed_since(LOOKBACK_MS):
        return None
    return not allowed_since(IDLE_WINDOW_MS)


def ask_ollama(question: dict, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
               timeout: float = MODEL_TIMEOUT) -> str:
    """The model's raw reply text. Raises OSError if Ollama can't be reached."""
    body = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0},
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
    worker = Classifier(store.connect(path), lambda question: ask_ollama(question, url, model))
    log.info("classifying unknown domains in %s with %s at %s", path, model, url)
    try:
        while True:
            try:
                stored = worker.run_once()
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


if __name__ == "__main__":
    main()
