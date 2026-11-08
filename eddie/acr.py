"""ACR blocker (#104): smart TVs reporting what's on screen.

Automatic content recognition (ACR) on a smart TV fingerprints what's on the
screen, from any input, and sends the fingerprints to the maker's ACR servers
every few seconds. Eddie never sees that payload, only the lookups, so it
treats a seeded ACR domain looked up on a regular timer (beacons.py) as ACR:

- The DNS server blocks it (decision.py), after rules, the allowlist and the
  blocklist, so a user rule or an essential still wins. Once a device's ACR
  domain has been seen beaconing it stays blocked for that device until the
  process restarts, so a TV that retries faster after the block doesn't
  unblock itself by breaking its own timer.
- The API (/ingest) pushes an acr_detected event (docs/EVENTS.md) when a
  query event for one of these domains was blocked by the DNS server and
  the API's own timing measured it as a beacon, at most once per device and
  domain every REPEAT_MS. The interval shown is that measurement; with no
  measured timer there is no event.

ACR is seeded from public sources, one per entry below. The bundled
blocklists (blocklists/smarttv.txt is Perflyst's PiHoleBlocklist SmartTV.txt)
already cover several of these names; the timer is what tells Eddie the
lookups are ACR rather than, say, a one-off config fetch. None has been
checked against a real TV on this network yet.
An entry covers the name and every subdomain of it.
"""

REPEAT_MS = 60 * 60_000  # the same device and domain again within this is one event
MAX_RECENT = 10_000  # (device, domain) keys remembered for REPEAT_MS

IMC24 = ("Anis et al., 'Watching TV with the Second-Party: A First Look at Automatic Content"
         " Recognition Tracking in Smart TVs', IMC 2024 (arXiv:2409.06203)")
PERFLYST = "Perflyst PiHoleBlocklist SmartTV.txt (blocklists/smarttv.txt)"

# name -> (ACR provider, source)
ACR = {
    "samsungacr.com": ("Samsung", f"{IMC24}; {PERFLYST}"),
    "alphonso.tv": ("LG (Alphonso)", f"{IMC24}; {PERFLYST}"),
    "tvinteractive.tv": ("Vizio (Inscape)", f"{PERFLYST} (control.tvinteractive.tv and others);"
                         " Inscape ACR described in FTC v. Vizio, 2017"),
    "samba.tv": ("Samba TV", f"{PERFLYST} (*.cid.samba.tv); StevenBlack hosts [samba.tv] section"),
}


def endpoint(domain: str) -> tuple[str, str] | None:
    """(the ACR name covering domain, its provider), or None."""
    labels = domain.strip().lower().rstrip(".").split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        if name in ACR:
            return name, ACR[name][0]
    return None


def every(seconds: int) -> str:
    if seconds < 120:
        return f"every {seconds} seconds"
    return f"about every {round(seconds / 60)} minutes"


def reason(device: str, domain: str, provider: str, interval_s: int) -> str:
    """The line shown on the dashboard. Only called for a lookup the DNS server blocked."""
    return (f"{device} was checking in with {provider}'s content recognition (ACR) server {domain} "
            f"{every(interval_s)}. ACR is how TVs report what's on screen. Blocked.")


class Sightings:
    """Turns blocked, beaconing query events for ACR domains into acr_detected events."""

    def __init__(self, repeat_ms: int = REPEAT_MS, max_recent: int = MAX_RECENT):
        self.repeat_ms = repeat_ms
        self.max_recent = max_recent
        self.last: dict[tuple[str, str], int] = {}

    def observe(self, row: dict, lookups: int = 0) -> dict | None:
        """row is a feed event with beacon_s set by the API; lookups is how many it was measured over."""
        if row.get("action") != "block" or not row.get("beacon_s"):
            return None
        found = endpoint(row["domain"])
        if not found:
            return None
        name, provider = found
        key = (row["device_mac"], row["domain"])
        ts = row["ts"]
        if ts - self.last.get(key, -self.repeat_ms) < self.repeat_ms:
            return None
        if len(self.last) >= self.max_recent:
            self.last = {k: t for k, t in self.last.items() if ts - t < self.repeat_ms}
            if len(self.last) >= self.max_recent:
                self.last.pop(next(iter(self.last)))
        self.last[key] = ts
        return {"type": "acr_detected", "ts": ts, "device_mac": row["device_mac"], "device": row["device"],
                "domain": row["domain"], "acr_name": name, "provider": provider,
                "interval_s": row["beacon_s"], "lookups": lookups, "source": row["source"],
                "reason": reason(row["device"], row["domain"], provider, row["beacon_s"])}
