"""Beacon and idle detection (#24): timing evidence that a lookup is telemetry.

Beacon: a device looking up the same domain on a timer. For each device and
domain the last HISTORY lookups are kept; when there are at least MIN_GAPS
gaps, the mean gap is between MIN_INTERVAL_MS and MAX_INTERVAL_MS and the
gaps' coefficient of variation (stdev / mean) is at most MAX_CV, the domain
is beaconing at about that mean interval. Lookups within MERGE_MS of the
previous one (an A and AAAA pair, a retry) count as one. Devices cache
answers, so a timer only shows here when the domain's TTL is shorter than it.

Idle: a lookup made when the device has had no allowed media traffic
(labels.HUMAN) for IDLE_MS. Unknown (None) until the device has had any.

Everything is in memory, bounded and per process: nothing is written to disk.
"""
import statistics
from collections import OrderedDict, deque

HISTORY = 12  # lookups kept per device and domain
MIN_GAPS = 5
MAX_CV = 0.15
MIN_INTERVAL_MS = 10_000
MAX_INTERVAL_MS = 3_600_000
MERGE_MS = 2_000
MAX_PAIRS = 10_000  # device and domain pairs remembered; least recently seen are dropped
IDLE_MS = 5 * 60 * 1000
MAX_DEVICES = 1_000


def interval_ms(timestamps) -> int | None:
    """The mean gap if these sorted lookup times are a regular beacon, else None."""
    ts = list(timestamps)
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    if len(gaps) < MIN_GAPS:
        return None
    mean = statistics.fmean(gaps)
    if not MIN_INTERVAL_MS <= mean <= MAX_INTERVAL_MS:
        return None
    return round(mean) if statistics.pstdev(gaps) / mean <= MAX_CV else None


class Beacons:
    def __init__(self, max_pairs: int = MAX_PAIRS):
        self.max_pairs = max_pairs
        self.seen: OrderedDict[tuple[str, str], deque[int]] = OrderedDict()

    def observe(self, device: str, domain: str, ts_ms: int) -> int | None:
        """Record a lookup; the beacon interval in ms if the pair is now regular, else None."""
        key = (device, domain)
        history = self.seen.get(key)
        if history is None:
            history = self.seen[key] = deque(maxlen=HISTORY)
            if len(self.seen) > self.max_pairs:
                self.seen.popitem(last=False)
        else:
            self.seen.move_to_end(key)
        if not history or ts_ms - history[-1] >= MERGE_MS:
            history.append(ts_ms)  # earlier (out-of-order) or merged lookups are not added
        return interval_ms(history)


class Idle:
    def __init__(self, max_devices: int = MAX_DEVICES):
        self.max_devices = max_devices
        self.last_media: OrderedDict[str, int] = OrderedDict()

    def observe(self, device: str, ts_ms: int, media: bool) -> bool | None:
        """Record a lookup; True if the device was idle when it made it, None if unknown."""
        last = self.last_media.get(device)
        if media:
            self.last_media[device] = max(ts_ms, last or ts_ms)
            self.last_media.move_to_end(device)
            if len(self.last_media) > self.max_devices:
                self.last_media.popitem(last=False)
            return False
        return None if last is None else ts_ms - last >= IDLE_MS
