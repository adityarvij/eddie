"""Bypass detection (#97): devices that may be sending DNS around Eddie.

An app or OS that uses encrypted DNS (DoH, DoT, Android's Private DNS) usually
looks its resolver up by name through Eddie first. A lookup of one of the names
in RESOLVERS is a sign the device may be sending some of its lookups straight
to that resolver, where Eddie can't see or block them. It's a suspicion, not
proof: a browser checks the resolver even when it ends up not using it, and a
device that uses hard-coded resolver IPs never asks Eddie at all, so no
lookup here ever shows a device is fully covered by Eddie.

The API (/ingest) checks every query event against RESOLVERS and pushes a
bypass_suspected event naming the device (docs/EVENTS.md), at most once per
device and resolver name every REPEAT_MS. Nothing runs on the DNS hot path.

EDDIE_BLOCK_DOH=on makes the DNS server block these names (decision.py), so a
client that set its resolver up by name falls back to Eddie. That only stops
DoH bootstrapping; it can't stop a client that has the resolver's IP.

RESOLVERS is seeded from the public resolver lists in the curl wiki
(https://github.com/curl/curl/wiki/DNS-over-HTTPS), Chromium's DoH provider
table (net/dns/public/doh_provider_entry.cc) and Firefox's default TRR
providers, plus the Private DNS names the providers publish for Android.
An entry covers the name and every subdomain of it (NextDNS profile IDs,
for example, are subdomains of dns.nextdns.io).
"""

REPEAT_MS = 10 * 60_000  # the same device and resolver name again within this is one event
MAX_RECENT = 10_000  # (device, name) keys remembered for REPEAT_MS

RESOLVERS = {
    "dns.google": "Google Public DNS",
    "cloudflare-dns.com": "Cloudflare",
    "one.one.one.one": "Cloudflare",
    "1dot1dot1dot1.cloudflare-dns.com": "Cloudflare",
    "dns.quad9.net": "Quad9",
    "dns9.quad9.net": "Quad9",
    "dns10.quad9.net": "Quad9",
    "dns11.quad9.net": "Quad9",
    "doh.opendns.com": "OpenDNS",
    "doh.familyshield.opendns.com": "OpenDNS",
    "dns.nextdns.io": "NextDNS",
    "doh.cleanbrowsing.org": "CleanBrowsing",
    "dns.adguard-dns.com": "AdGuard DNS",
    "dns.adguard.com": "AdGuard DNS",
    "dns-family.adguard.com": "AdGuard DNS",
    "dns-unfiltered.adguard.com": "AdGuard DNS",
    "doh.dns.sb": "DNS.SB",
    "dns.mullvad.net": "Mullvad",
    "doh.mullvad.net": "Mullvad",
    "freedns.controld.com": "Control D",
    "doh.xfinity.com": "Xfinity",
    "dns.alidns.com": "AliDNS",
    "doh.pub": "DNSPod",
}


def resolver(domain: str) -> tuple[str, str] | None:
    """(the RESOLVERS name covering domain, its provider), or None."""
    labels = domain.strip().lower().rstrip(".").split(".")
    for i in range(len(labels) - 1):
        name = ".".join(labels[i:])
        if name in RESOLVERS:
            return name, RESOLVERS[name]
    return None


def reason(device: str, domain: str, provider: str, blocked: bool) -> str:
    """The line shown on the dashboard. Says what was seen, never that the device is covered."""
    text = (f"{device} looked up {domain} ({provider}), an encrypted DNS resolver. "
            "It may be sending some lookups there, where Eddie can't see or block them.")
    if blocked:
        text += (" Eddie blocked the name, which only stops encrypted DNS set up by name;"
                 " an app with the resolver's IP still skips Eddie.")
    return text


class Suspicions:
    """Turns query events into bypass_suspected events, once per device and name per REPEAT_MS."""

    def __init__(self, repeat_ms: int = REPEAT_MS, max_recent: int = MAX_RECENT):
        self.repeat_ms = repeat_ms
        self.max_recent = max_recent
        self.last: dict[tuple[str, str], int] = {}  # (device_mac, resolver name) -> ts

    def observe(self, event: dict) -> dict | None:
        """The bypass_suspected event for a query event, or None."""
        found = resolver(event["domain"])
        if not found:
            return None
        name, provider = found
        key = (event["device_mac"], name)
        last = self.last.get(key)
        if last is not None and 0 <= event["ts"] - last < self.repeat_ms:
            return None
        if len(self.last) >= self.max_recent:
            self.last = {k: t for k, t in self.last.items() if event["ts"] - t < self.repeat_ms}
            if len(self.last) >= self.max_recent:
                self.last.clear()
        self.last[key] = event["ts"]
        blocked = event["action"] == "block"
        return {"type": "bypass_suspected", "ts": event["ts"], "device_mac": event["device_mac"],
                "device": event["device"], "domain": event["domain"], "resolver": provider,
                "blocked": blocked, "reason": reason(event["device"], event["domain"], provider, blocked)}
