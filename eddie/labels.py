"""Hand-written domain table: who owns a domain and what kind of traffic it is.

Matching is by suffix, most specific first, so "device-metrics-us.amazon.com"
hits its own entry before falling back to "amazon.com".
"""

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


def lookup(domain: str) -> tuple[str, str]:
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        hit = DOMAINS.get(".".join(parts[i:]))
        if hit:
            return hit
    return (".".join(parts[-2:]), "unknown")
