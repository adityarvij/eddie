"""Loads the blocklists and the essentials allowlist for the decision engine.

Blocklists are every blocklists/*.txt file, in hosts format ("0.0.0.0 a.com")
or one domain per line. allowlist.txt holds the essentials that must keep
working (video CDNs, sign-in, updates, time sync, captive-portal checks). Both
are read once at DNS startup; an entry covers the domain and its subdomains,
and the allowlist wins over the blocklist (decision.py).

Keep allowlist entries narrow: allowing a whole company domain such as
amazon.com would also allow its trackers (device-metrics-us.amazon.com).
"""
import ipaddress
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCKLIST_DIR = ROOT / "blocklists"
ALLOWLIST = ROOT / "allowlist.txt"
# Names hosts files map to themselves; never real lookups worth blocking.
NOT_DOMAINS = {"localhost", "localhost.localdomain", "local", "broadcasthost", "0.0.0.0"}
DOMAIN = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)+")


def _is_ip(token: str) -> bool:
    try:
        ipaddress.ip_address(token.split("%")[0])
        return True
    except ValueError:
        return False


def parse(lines) -> set[str]:
    """Domains in hosts-format or plain-domain lines. Comments and junk are skipped."""
    domains = set()
    for line in lines:
        tokens = line.split("#", 1)[0].lower().split()
        if tokens and _is_ip(tokens[0]):
            tokens = tokens[1:]
        for token in tokens:
            token = token.rstrip(".")
            if token not in NOT_DOMAINS and not _is_ip(token) and DOMAIN.fullmatch(token):
                domains.add(token)
    return domains


def read(path: Path) -> set[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return parse(f)


def load(blocklist_dir: Path = BLOCKLIST_DIR, allowlist: Path = ALLOWLIST) -> tuple[set[str], set[str]]:
    """(allowlist, blocklist). A missing allowlist is an error: blocking without it can brick devices."""
    blocked = set()
    for path in sorted(Path(blocklist_dir).glob("*.txt")):
        blocked |= read(path)
    return read(Path(allowlist)), blocked


def load_from_env() -> tuple[set[str], set[str]]:
    """EDDIE_BLOCKLISTS (directory, or "off" for none) and EDDIE_ALLOWLIST (file)."""
    allow_path = Path(os.environ.get("EDDIE_ALLOWLIST", ALLOWLIST))
    block_dir = os.environ.get("EDDIE_BLOCKLISTS", str(BLOCKLIST_DIR))
    if block_dir == "off":
        return read(allow_path), set()
    return load(Path(block_dir), allow_path)
