"""A seeded, invented week of household traffic for the full "what they know" report.

This is demo data, not anyone's house. Every event carries "demo": True and
every device name ends in "(demo)", so it can't pass for real traffic in the
API, the dashboard or speech. It is generated in memory on request, never
written to eddie.db and never mixed into the live feed.

The schedule (local time) is chosen so the report has something to find:
- weekdays: the TV streams 7 to about 10:30 PM; the house is quiet 8 to 6;
- Friday night into Saturday: a late session after midnight;
- weekends: cartoons on the tablet in the morning, plus the evening TV;
- the TV and tablet send telemetry and ad lookups around the clock.

Other devices cover the report categories a lone Fire Stick can't show live
(#134): a kids' app on the tablet; a phone for pharmacy, telehealth, banking,
shopping, news, church, politics and job hunting; a laptop on work tools
before 8 AM; a kitchen speaker with a voice assistant and music; a thermostat;
a doorbell; and one ad ID syncing company seen from the TV, tablet and phone.
They come from their own seeded stream (EXTRA_SEED), so the TV and tablet
week above is the same with or without them, and none of them streams video,
so the house still looks empty on weekdays 8 to 6.
"""
import random
from datetime import datetime, timedelta

from . import labels

SEED = 33
EXTRA_SEED = 34
LABEL = "Demo data: an invented, seeded week. Not your household's traffic."
TV = "Living room TV (demo)"
TABLET = "Kids' tablet (demo)"
PHONE = "Phone (demo)"
LAPTOP = "Laptop (demo)"
SPEAKER = "Kitchen speaker (demo)"
THERMOSTAT = "Thermostat (demo)"
DOORBELL = "Front doorbell (demo)"
MIN = 60 * 1000

# (device, domain, action, reason, mean minutes between lookups)
BACKGROUND = [
    (TV, "device-metrics-us.amazon.com", "block", "On the Amazon Fire TV tracker list", 6),
    (TV, "unagi-na.amazon.com", "block", "Amazon device analytics", 15),
    (TV, "pool.ntp.org", "allow", "Clock sync", 60),
    (TABLET, "crashlytics.com", "block", "Google crash telemetry", 20),
]
WATCHING = [  # while a session is on
    ("nflxvideo.net", "allow", "Netflix video", 2),
    ("api-global.netflix.com", "allow", "Netflix app", 5),
    ("mads.amazon-adsystem.com", "block", "Amazon ad network", 8),
]
EXTRA_BACKGROUND = [
    (THERMOSTAT, "api.ecobee.com", "allow", "Thermostat cloud", 15),
    (DOORBELL, "api.skybell.com", "allow", "Doorbell cloud", 10),
    (TV, "idsync.rlcdn.com", "block", "LiveRamp ID sync", 240),
    (TABLET, "idsync.rlcdn.com", "block", "LiveRamp ID sync", 240),
    (PHONE, "idsync.rlcdn.com", "block", "LiveRamp ID sync", 240),
]
# (weekdays (0 = Monday), start hour, end hour, device, [(domain, action, reason, mean minutes)])
ROUTINE = [
    (range(5), 7, 8, LAPTOP, [("app.slack.com", "allow", "Slack", 3), ("zoom.us", "allow", "Zoom", 10)]),
    (range(7), 7, 7.5, PHONE, [("www.nytimes.com", "allow", "News", 3)]),
    (range(7), 7, 7.75, SPEAKER, [("avs-alexa-na.amazon.com", "allow", "Alexa voice service", 5),
                                  ("spclient.wg.spotify.com", "allow", "Spotify", 4)]),
    (range(7), 20, 20.25, PHONE, [("secure.chase.com", "allow", "Banking", 3)]),
    ((1, 3), 21, 21.5, PHONE, [("www.indeed.com", "allow", "Job search", 3)]),
    ((2,), 21, 21.3, PHONE, [("www.goodrx.com", "allow", "Pharmacy prices", 3)]),
    ((0,), 18, 18.5, PHONE, [("member.teladoc.com", "allow", "Telehealth", 4)]),
    ((3,), 21.5, 21.75, PHONE, [("ballotpedia.org", "allow", "Elections", 3)]),
    ((5,), 11, 12, PHONE, [("www.target.com", "allow", "Shopping", 3)]),
    ((5, 6), 8.5, 10.5, TABLET, [("www.youtubekids.com", "allow", "YouTube Kids", 4)]),
    ((6,), 9.5, 10.5, PHONE, [("www.biblegateway.com", "allow", "Bible reading", 5)]),
]
CARTOONS = [
    ("youtube.com", "allow", "YouTube", 2),
    ("googlevideo.com", "allow", "YouTube video", 2),
    ("doubleclick.net", "block", "Google ad network", 10),
]


def _event(ts: int, device: str, domain: str, action: str, reason: str) -> dict:
    company, category = labels.lookup(domain)
    return {"ts": ts, "device": device, "domain": domain, "action": action, "reason": reason,
            "source": "demo", "company": company, "category": category, "demo": True}


def _stream(rng: random.Random, start: int, end: int, every_min: float):
    t = start + rng.uniform(0, every_min) * MIN
    while t < end:
        yield int(t)
        t += rng.uniform(0.5, 1.5) * every_min * MIN


def _session(rng, out, device, streams, start, end):
    for domain, action, reason, every in streams:
        out.extend(_event(t, device, domain, action, reason) for t in _stream(rng, start, end, every))


def events(now_ms: int, seed: int = SEED) -> list[dict]:
    """The seven days up to now_ms, sorted by time. Same seed and day, same week."""
    rng = random.Random(seed)
    end_day = datetime.fromtimestamp(now_ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    start = int((end_day - timedelta(days=6)).timestamp() * 1000)
    out: list[dict] = []
    for device, domain, action, reason, every in BACKGROUND:
        out.extend(_event(t, device, domain, action, reason) for t in _stream(rng, start, now_ms, every))

    def at(day: datetime, hour: float, jitter_min: int = 15) -> int:
        return int((day + timedelta(hours=hour)).timestamp() * 1000) + rng.randint(-jitter_min, jitter_min) * MIN

    for offset in range(7):
        day = end_day - timedelta(days=6 - offset)
        if day.weekday() < 5:
            _session(rng, out, TV, WATCHING, at(day, 19), at(day, 22.5))
        else:
            _session(rng, out, TABLET, CARTOONS, at(day, 8.5), at(day, 10.5))
            _session(rng, out, TV, WATCHING, at(day, 18.5), at(day, 23.5))
        if day.weekday() == 5:  # Friday night runs past midnight into Saturday
            _session(rng, out, TV, WATCHING, at(day, 0.5, 5), at(day, 1.75, 5))
    out.extend(_extras(random.Random(seed + EXTRA_SEED - SEED), end_day, start, now_ms))
    return sorted((e for e in out if e["ts"] <= now_ms), key=lambda e: e["ts"])


def _extras(rng: random.Random, end_day: datetime, start: int, now_ms: int) -> list[dict]:
    """The other devices (#134): EXTRA_BACKGROUND around the clock, ROUTINE on its days."""
    out: list[dict] = []
    for device, domain, action, reason, every in EXTRA_BACKGROUND:
        out.extend(_event(t, device, domain, action, reason) for t in _stream(rng, start, now_ms, every))
    for offset in range(7):
        day = end_day - timedelta(days=6 - offset)
        for weekdays, begin, end, device, streams in ROUTINE:
            if day.weekday() in weekdays:
                ms = [int((day + timedelta(hours=h)).timestamp() * 1000) for h in (begin, end)]
                _session(rng, out, device, streams, *ms)
    return out
