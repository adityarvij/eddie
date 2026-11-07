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
    # Misc
    "pool.ntp.org": ("NTP", "time"),
}

TRACKING = {"telemetry", "ads", "analytics", "tracking"}
HUMAN = {"streaming"}  # traffic that means a person is using the device


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
