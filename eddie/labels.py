"""Hand-written domain table: who owns a domain and what kind of traffic it is.

Matching is by suffix, most specific first, so "device-metrics-us.amazon.com"
hits its own entry before falling back to "amazon.com".
"""

import ipaddress
import json
from pathlib import Path
import tldextract

# Disable both network fetching and filesystem cache writes.
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)
OWNERS = json.loads((Path(__file__).parent / 'data' / 'tracker_owners.json').read_text())
# Tracker domains of registered data brokers and ad exchanges (see eddie/data/README.md).
BROKERS = json.loads((Path(__file__).parent / 'data' / 'brokers.json').read_text())

# suffix -> (company, category)
DOMAINS = {
    # Amazon
    "device-metrics-us.amazon.com": ("Amazon", "telemetry"),
    "unagi-na.amazon.com": ("Amazon", "telemetry"),
    "dcape-na.amazon.com": ("Amazon", "telemetry"),
    "arcus-uswest.amazon.com": ("Amazon", "telemetry"),
    "amazon-adsystem.com": ("Amazon", "ads"),
    "api.amazonvideo.com": ("Amazon", "streaming"),
    "atv-ps.amazon.com": ("Amazon", "streaming"),
    "aiv-cdn.net": ("Amazon", "streaming"),
    "softwareupdates.amazon.com": ("Amazon", "updates"),
    "api.amazon.com": ("Amazon", "sign-in"),
    "firetvcaptiveportal.com": ("Amazon", "connectivity"),
    "amazonaws.com": ("Amazon", "cloud"),
    "amazon.com": ("Amazon", "services"),
    # Netflix
    "nflxvideo.net": ("Netflix", "streaming"),
    "nflxso.net": ("Netflix", "streaming"),
    "nflxext.com": ("Netflix", "streaming"),
    "netflix.com": ("Netflix", "streaming"),
    # Google
    "googlevideo.com": ("Google", "streaming"),
    "youtube.com": ("Google", "streaming"),
    "app-measurement.com": ("Google", "analytics"),
    "crashlytics.com": ("Google", "telemetry"),
    "doubleclick.net": ("Google", "ads"),
    "google-analytics.com": ("Google", "analytics"),
    # Meta
    "graph.facebook.com": ("Meta", "tracking"),
    "facebook.com": ("Meta", "social"),
    # Apple
    "icloud.com": ("Apple", "services"),
    "apple.com": ("Apple", "services"),
    # Ring (home mode pitch)
    "ring.com": ("Ring", "camera"),
    "arlo.com": ("Arlo", "camera"),
    "simplisafe.com": ("SimpliSafe", "alarm"),
    # Misc
    "pool.ntp.org": ("NTP", "time"),
    # Inference topics (#134); each service's own public domain, see eddie/data/README.md.
    # Kids
    "youtubekids.com": ("YouTube Kids", "kids"),
    "pbskids.org": ("PBS Kids", "kids"),
    "abcmouse.com": ("ABCmouse", "kids"),
    "roblox.com": ("Roblox", "kids"),
    # Music
    "spotify.com": ("Spotify", "music"),
    "scdn.co": ("Spotify", "music"),
    "pandora.com": ("Pandora", "music"),
    "music.apple.com": ("Apple Music", "music"),
    # Doorbells and thermostats
    "skybell.com": ("SkyBell", "doorbell"),
    "ecobee.com": ("ecobee", "thermostat"),
    "tccna.honeywell.com": ("Honeywell Home", "thermostat"),
    # Health, pharmacy and telehealth
    "goodrx.com": ("GoodRx", "health"),
    "cvs.com": ("CVS", "health"),
    "walgreens.com": ("Walgreens", "health"),
    "teladoc.com": ("Teladoc", "health"),
    "betterhelp.com": ("BetterHelp", "health"),
    "myfitnesspal.com": ("MyFitnessPal", "health"),
    # Banking, payments and credit
    "chase.com": ("Chase", "finance"),
    "venmo.com": ("Venmo", "finance"),
    "affirm.com": ("Affirm", "finance"),
    "creditkarma.com": ("Credit Karma", "finance"),
    # Shopping
    "target.com": ("Target", "shopping"),
    "walmart.com": ("Walmart", "shopping"),
    "ebay.com": ("eBay", "shopping"),
    # Work tools and job sites
    "slack.com": ("Slack", "work"),
    "zoom.us": ("Zoom", "work"),
    "teams.microsoft.com": ("Microsoft Teams", "work"),
    "indeed.com": ("Indeed", "jobs"),
    "glassdoor.com": ("Glassdoor", "jobs"),
    "ziprecruiter.com": ("ZipRecruiter", "jobs"),
    # News, religion and politics
    "nytimes.com": ("The New York Times", "news"),
    "foxnews.com": ("Fox News", "news"),
    "cnn.com": ("CNN", "news"),
    "npr.org": ("NPR", "news"),
    "biblegateway.com": ("Bible Gateway", "religion"),
    "quran.com": ("Quran.com", "religion"),
    "chabad.org": ("Chabad.org", "religion"),
    "democrats.org": ("Democratic Party", "politics"),
    "gop.com": ("Republican Party", "politics"),
    "ballotpedia.org": ("Ballotpedia", "politics"),
    # Voice assistants
    "avs-alexa-na.amazon.com": ("Amazon", "assistant"),
    "alexa.na.gateway.devices.a2z.com": ("Amazon", "assistant"),
    "guzzoni.apple.com": ("Apple", "assistant"),
    # Ad ID syncing: matching one person's IDs across companies and devices
    "rlcdn.com": ("LiveRamp", "id-sync"),
    "id5-sync.com": ("ID5", "id-sync"),
    "tapad.com": ("Tapad", "id-sync"),
    "uidapi.com": ("The Trade Desk", "id-sync"),
}

TRACKING = {"telemetry", "ads", "analytics", "tracking", "id-sync"}
HUMAN = {"streaming"}  # traffic that means a person is using the device
SECURITY = {"camera", "doorbell", "alarm"}  # devices protecting the house; Eddie advises never blocking them
# Never kept raw past a day (#111): category -> the group store.forget counts it under.
SENSITIVE = {"health": "health", "kids": "kids", "news": "beliefs", "religion": "beliefs", "politics": "beliefs"}


def registrable_domain(domain: str) -> str:
    domain = domain.strip().lower().rstrip('.')
    try:
        return str(ipaddress.ip_address(domain))
    except ValueError:
        pass
    result = _EXTRACT(domain)
    return result.top_domain_under_public_suffix or domain


def lookup(domain: str) -> tuple[str, str]:
    domain = domain.strip().lower().rstrip('.')
    parts = domain.split('.')
    # Keep precise local categories even when the public ownership source
    # only knows the parent company. Match label boundaries, never substrings.
    category = 'unknown'
    company = None
    for i in range(len(parts)):
        suffix = '.'.join(parts[i:])
        if suffix in DOMAINS:
            company, category = DOMAINS[suffix]
            break
    if company is None:
        for i in range(len(parts)):
            owner = OWNERS.get('.'.join(parts[i:]))
            if owner:
                company = owner
                break
    return company or registrable_domain(domain), category


def id_sync(domain: str) -> str | None:
    """The ad-ID sync domain (a DOMAINS key) this lookup belongs to, else None."""
    parts = domain.strip().lower().rstrip('.').split('.')
    for i in range(len(parts)):
        suffix = '.'.join(parts[i:])
        if suffix in DOMAINS:
            return suffix if DOMAINS[suffix][1] == 'id-sync' else None
    return None


def broker(domain: str) -> dict | None:
    """{company, kind} when the domain belongs to a known data broker or ad exchange."""
    parts = domain.strip().lower().rstrip('.').split('.')
    for i in range(len(parts)):
        if entry := BROKERS.get('.'.join(parts[i:])):
            return entry
    return None
