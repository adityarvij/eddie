"""Fake Fire Stick traffic in the exact event format from docs/EVENTS.md.

Stands in for Will's DNS server until real traffic flows. It mimics his
decision engine too, including home mode, so the screen behaves like the demo.

    uv run python scripts/fake_traffic.py              # real time
    uv run python scripts/fake_traffic.py --speed 4    # faster, for building the UI
    uv run python scripts/fake_traffic.py --scene idle # TV on, nobody watching

Stdlib only, so it also runs on the Pi as-is.
"""
import argparse
import json
import random
import sys
import time
import urllib.request
from dataclasses import dataclass

FIRE_STICK = ("f0:4f:7c:11:22:33", "Fire Stick")
IPHONE = ("ip:172.20.10.3", "Adi's iPhone")

# Scenes: the TV sits idle (telemetry only), then someone watches Netflix and Prime.
CYCLE = [("idle", 60), ("watching", 180)]


@dataclass
class Stream:
    device: tuple[str, str]
    domain: str
    every_s: float
    jitter: float  # fraction of every_s
    scene: str  # "always" | "watching"
    action: str
    source: str
    reason: str
    essential: bool = False  # never blocked by home mode
    due: float = 0.0


def streams() -> list[Stream]:
    S = Stream
    return [
        # Telemetry that runs whether or not anyone is watching
        S(FIRE_STICK, "device-metrics-us.amazon.com", 30, 0.03, "always", "block", "blocklist", "On the Amazon Fire TV tracker list"),
        S(FIRE_STICK, "unagi-na.amazon.com", 60, 0.04, "always", "block", "blocklist", "Amazon device analytics"),
        S(FIRE_STICK, "dcape-na.amazon.com", 45, 0.1, "always", "block", "ai", "AI: tracker (0.86), usage reports to Amazon"),
        S(FIRE_STICK, "arcus-uswest.amazon.com", 90, 0.3, "always", "flag", "default", "Unknown, sent to AI for a verdict"),
        S(FIRE_STICK, "spectrum.s3.amazonaws.com", 120, 0.4, "always", "flag", "default", "Unknown, sent to AI for a verdict"),
        S(FIRE_STICK, "firetvcaptiveportal.com", 60, 0.2, "always", "allow", "allowlist", "Connectivity check", essential=True),
        S(FIRE_STICK, "softwareupdates.amazon.com", 240, 0.3, "always", "allow", "allowlist", "Fire OS updates", essential=True),
        S(FIRE_STICK, "pool.ntp.org", 300, 0.2, "always", "allow", "allowlist", "Clock sync", essential=True),
        # Only while someone is watching
        S(FIRE_STICK, "api-global.netflix.com", 12, 0.5, "watching", "allow", "allowlist", "Netflix app", essential=True),
        S(FIRE_STICK, "ipv4-c021-bos001-ix.1.oca.nflxvideo.net", 8, 0.5, "watching", "allow", "allowlist", "Netflix video", essential=True),
        S(FIRE_STICK, "occ-0-1168-299.1.nflxso.net", 15, 0.6, "watching", "allow", "allowlist", "Netflix artwork", essential=True),
        S(FIRE_STICK, "api.amazonvideo.com", 25, 0.5, "watching", "allow", "allowlist", "Prime Video app", essential=True),
        S(FIRE_STICK, "mads.amazon-adsystem.com", 20, 0.5, "watching", "block", "blocklist", "Amazon ad network"),
        S(FIRE_STICK, "aax-us-east.amazon-adsystem.com", 28, 0.5, "watching", "block", "blocklist", "Amazon ad exchange"),
        S(FIRE_STICK, "app-measurement.com", 40, 0.4, "watching", "block", "blocklist", "Google Analytics for Firebase"),
        S(FIRE_STICK, "settings.crashlytics.com", 90, 0.3, "watching", "block", "heuristic", "Name contains 'crashlytics'"),
        # A phone on the network, for presence and variety
        S(IPHONE, "gateway.icloud.com", 45, 0.5, "always", "allow", "allowlist", "Apple services", essential=True),
        S(IPHONE, "graph.facebook.com", 70, 0.5, "always", "block", "blocklist", "Facebook tracking"),
    ]


def decide(s: Stream, home_mode: bool) -> tuple[str, str, str]:
    """Same order as the real engine, with home mode layered on top."""
    if home_mode and s.device == FIRE_STICK and not s.essential and s.action != "block":
        return "block", "home_mode", "Home mode: the TV can't phone home while you're home"
    return s.action, s.source, s.reason


def post(url: str, path: str, body=None):
    req = urllib.request.Request(
        url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        return json.loads(r.read())


def scene_at(t: float, fixed: str | None) -> str:
    if fixed:
        return fixed
    t %= sum(d for _, d in CYCLE)
    for name, dur in CYCLE:
        if t < dur:
            return name
        t -= dur
    return CYCLE[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--speed", type=float, default=1.0, help="time multiplier (4 = four times faster)")
    ap.add_argument("--scene", choices=["idle", "watching"], help="stay in one scene instead of cycling")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    all_streams = streams()
    for s in all_streams:  # stagger so everything doesn't fire at t=0
        s.due = random.uniform(0, min(s.every_s, 10))

    start = time.time()
    home_mode, last_mode_check, last_scene = False, 0.0, None
    print(f"Sending fake traffic to {args.url} at {args.speed}x. Ctrl-C to stop.")

    while True:
        t = (time.time() - start) * args.speed  # scene clock
        scene = scene_at(t, args.scene)
        if scene != last_scene:
            print(f"--- scene: {scene}")
            last_scene = scene

        if time.time() - last_mode_check > 2:
            try:
                home_mode = post(args.url, "/mode")["home_mode"]
            except Exception:
                pass
            last_mode_check = time.time()

        batch = []
        for s in all_streams:
            if t < s.due:
                continue
            s.due = t + s.every_s * (1 + random.uniform(-s.jitter, s.jitter))
            if s.scene not in ("always", scene):
                continue
            action, source, reason = decide(s, home_mode)
            batch.append({
                "type": "query",
                "ts": int(time.time() * 1000),
                "device_mac": s.device[0],
                "device": s.device[1],
                "domain": s.domain,
                "qtype": "A",
                "action": action,
                "source": source,
                "reason": reason,
            })

        if batch:
            try:
                post(args.url, "/ingest", batch)
                if not args.quiet:
                    for e in batch:
                        print(f"{e['device']:>14} → {e['domain']:<42} {e['action']}")
            except Exception as err:
                print(f"API not reachable ({err}); is uvicorn running?", file=sys.stderr)
        time.sleep(0.25)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
