"""Privacy settings coach (#112): which built-in settings to change on each recognised device.

Hand-written guides, one per device type, each step taken from the maker's own
help pages listed in its sources. Eddie only reads them out: it never signs in
to a device account or changes a setting, so the guide is all it offers.

A device is recognised from what Eddie already holds about it: its label or
DHCP hostname ("Fire Stick", "Front Door Ring"), its MAC vendor (devices.py)
and the domains it looked up in the window. Nothing is stored; the list is
worked out from eddie.db on each request.
"""
import re
import sqlite3

from . import devices

WEEK_MS = 7 * 24 * 3_600_000

AMAZON_FIRE_TV_FAQ = {
    "title": "Amazon: Privacy settings FAQs for Fire TV devices, Fire TV Edition devices, Fire tablets and Kindle e-readers",
    "url": "https://www.amazon.com/gp/help/customer/display.html?nodeId=GQFYXZHZB2H629WN",
}
RING_CONTROL_CENTER = {
    "title": "Ring: Ring Control Center information",
    "url": "https://ring.com/us/en/support/articles/rj2pk/Ring-Control-Center-Information",
}
RING_E2EE_ROLLOUT = {
    "title": "TechHive: Ring rolls out end-to-end encryption worldwide",
    "url": "https://www.techhive.com/article/579537/ring-rolls-out-end-to-end-encryption-worldwide.html",
}

# kind -> guide. "spoken" is what voice reads; "steps" and "sources" are for the dashboard.
GUIDES = {
    "fire_tv": {
        "name": "Fire TV",
        "where": "On the Fire TV: Settings > Preferences > Privacy Settings",
        "spoken": ("on the TV open Settings, Preferences, Privacy Settings and turn off Device Usage Data, "
                   "Collect App Usage Data and Interest-based Ads"),
        "steps": [
            {"setting": "Device Usage Data", "do": "Turn off",
             "why": "Amazon stops using the device's usage data for marketing offers and product improvement."},
            {"setting": "Collect App Usage Data", "do": "Turn off",
             "why": "Amazon stops collecting when other companies' apps are opened and closed and how long they're used."},
            {"setting": "Interest-based Ads", "do": "Turn off",
             "why": "Apps can't use the advertising ID to build an ad profile. Ads still show, just not targeted."},
            {"setting": "Over-the-Air viewing data (Fire TV smart TVs only)", "do": "Turn off",
             "why": "On a TV with Fire TV built in, stops sharing what you watch on antenna channels."},
        ],
        "note": "Each Fire TV has its own settings, so repeat this on every one.",
        "sources": [AMAZON_FIRE_TV_FAQ],
    },
    "ring": {
        "name": "Ring",
        "where": "In the Ring app: Menu > Control Center",
        "spoken": ("in the Ring app open Control Center: keep two-step verification on, turn on end-to-end "
                   "encryption, and remove shared users and signed-in devices you don't recognise"),
        "steps": [
            {"setting": "Account Verification", "do": "Check",
             "why": "Two-step verification sends a 6-digit code at sign-in; make sure it goes to a phone or email only you use."},
            {"setting": "Authorized Client Devices", "do": "Remove old ones",
             "why": "Every phone, tablet and computer that can sign in to the account is listed; remove any you don't recognise."},
            {"setting": "Shared Users", "do": "Remove old ones",
             "why": "Shared users can watch, save and share your videos; remove anyone who no longer needs that."},
            {"setting": "Video Encryption > End-to-End Encryption", "do": "Turn on",
             "why": "Only your enrolled phones can watch the videos, on compatible devices. Some app features stop working with it on."},
            {"setting": "Amazon Sidewalk", "do": "Turn off if unwanted",
             "why": "Ring turns it on by default; it can be turned off here."},
        ],
        "note": "Changing the account password also signs out every authorized device.",
        "sources": [RING_CONTROL_CENTER, RING_E2EE_ROLLOUT],
    },
}

_FIRE_TV_WORDS = re.compile(r"\b(?:fire ?tv|fire ?stick|fire ?cube)\b")
_TV_WORD = re.compile(r"\btv\b")
_RING_WORDS = re.compile(r"\bring\b")
# Domain suffixes only one kind of device looks up.
_FIRE_TV_DOMAINS = ("firetvcaptiveportal.com",)
_RING_DOMAINS = ("ring.com",)


def _words(*names: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (n or "").lower()) for n in names)


def _looked_up(domains: set[str], suffixes: tuple[str, ...]) -> bool:
    return any(d == s or d.endswith("." + s) for d in domains for s in suffixes)


def kind(label: str | None, hostname: str | None, vendor: str | None, domains: set[str]) -> str | None:
    """The GUIDES key for a device, or None. A name the household gave wins over vendor and lookups."""
    words = _words(label, hostname)
    if _RING_WORDS.search(words):
        return "ring"
    if _FIRE_TV_WORDS.search(words):
        return "fire_tv"
    if vendor == "Ring" or _looked_up(domains, _RING_DOMAINS):
        return "ring"
    if _looked_up(domains, _FIRE_TV_DOMAINS) or (vendor == "Amazon" and _TV_WORD.search(words)):
        return "fire_tv"
    return None


def suggestions(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    """[{key, name, kind}] for every device with a guide, from the devices table and the query log since since_ms."""
    domains: dict[str, set[str]] = {}
    for key, domain in conn.execute("SELECT DISTINCT device_mac, domain FROM queries WHERE ts >= ?", (since_ms,)):
        domains.setdefault(key, set()).add(domain.lower().rstrip("."))
    known = {r[0]: r[1:] for r in conn.execute("SELECT mac, label, hostname, vendor FROM devices")}
    out = []
    for key in known.keys() | domains.keys():
        label, hostname, vendor = known.get(key, (None, None, None))
        found = kind(label, hostname, vendor, domains.get(key, set()))
        if found:
            out.append({"key": key, "name": devices.display_name(key, label, hostname, vendor), "kind": found})
    return sorted(out, key=lambda d: (d["kind"], d["name"]))


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def spoken_text(found: list[dict]) -> str:
    if not found:
        return (f"I have privacy guides for {_join([g['name'] for g in GUIDES.values()])}, but I haven't "
                "recognised one on your network this week. Naming a device in Eddie helps me spot it.")
    parts = []
    for k, guide in GUIDES.items():
        names = [d["name"] for d in found if d["kind"] == k]
        if names:
            parts.append(f"For {_join(names)}, {guide['spoken']}.")
    return " ".join(parts + ["I haven't changed any of these myself. The steps and sources are under suggestions on the dashboard."])
