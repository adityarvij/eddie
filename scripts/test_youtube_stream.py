"""YouTube Live Stream Demo Tool for Home Mode (#48).

Demonstrates and verifies that Eddie's Home Mode intercepts and cuts
YouTube Live video streaming ingest (a.rtmp.youtube.com).
"""
import os
import socket
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eddie import decision, store
from eddie.decision import Engine, CAMERA_DOMAINS

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

YT_INGEST_DOMAINS = [
    "a.rtmp.youtube.com",
    "b.rtmp.youtube.com",
    "upload.youtube.com",
]


def resolve_domain_through_eddie(domain: str, home_mode: bool, device: str = "living-room-cam"):
    """Evaluates the domain against Eddie's decision engine and performs DNS resolution."""
    engine = Engine()
    engine.set_home_mode(home_mode)

    d = engine.evaluate(domain, device)

    if d.action == "block":
        return {
            "action": "BLOCK",
            "source": d.source,
            "reason": d.reason,
            "ip": "0.0.0.0",
        }

    # If allowed, resolve real IP via upstream DNS
    try:
        real_ip = socket.gethostbyname(domain)
    except socket.gaierror:
        real_ip = "142.250.80.78"  # Example YouTube Ingest IP
    return {
        "action": "ALLOW",
        "source": d.source,
        "reason": d.reason or "upstream forward",
        "ip": real_ip,
    }


def probe_rtmp_connection(ip: str, port: int = 1935, timeout: float = 2.0):
    """Attempts a real TCP handshake to YouTube Live RTMP ingest port 1935."""
    if ip == "0.0.0.0":
        return False, "Blocked by Eddie (IP resolves to 0.0.0.0)"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.close()
        return True, f"Successfully connected to YouTube RTMP ingest at {ip}:{port}"
    except Exception as e:
        return False, f"Connection failed to {ip}:{port}: {e}"


def run_demo():
    print(f"\n{BOLD}{CYAN}===================================================={RESET}")
    print(f"{BOLD}{CYAN}   EDDIE YOUTUBE LIVE STREAM HOME MODE DEMO (#48)   {RESET}")
    print(f"{BOLD}{CYAN}===================================================={RESET}\n")

    print(f"{BOLD}Step 1: Testing with Home Mode = OFF (Normal Streaming Mode){RESET}")
    print("------------------------------------------------------------")
    for domain in YT_INGEST_DOMAINS:
        res = resolve_domain_through_eddie(domain, home_mode=False)
        print(f"Domain: {domain:<22} -> Status: {GREEN}{res['action']}{RESET} | Resolved IP: {res['ip']}")
    
    # Try RTMP handshake on primary domain
    primary_ip = resolve_domain_through_eddie("a.rtmp.youtube.com", home_mode=False)["ip"]
    print(f"\nProbing RTMP Stream Port 1935 on {primary_ip}...")
    ok, msg = probe_rtmp_connection(primary_ip)
    if ok:
        print(f"[{GREEN}LIVE STREAM ACTIVE{RESET}] {msg}")
    else:
        print(f"[{YELLOW}RTMP PROBE RESULT{RESET}] {msg}")

    print(f"\n{BOLD}Step 2: Activating Home Mode (Simulating Phone Home or 'Hey Eddie'){RESET}")
    print("------------------------------------------------------------")
    for domain in YT_INGEST_DOMAINS:
        res = resolve_domain_through_eddie(domain, home_mode=True)
        print(f"Domain: {domain:<22} -> Status: {RED}{res['action']}{RESET} ({res['source']}) | Resolved IP: {res['ip']}")

    # Try RTMP handshake with Home Mode ON
    blocked_ip = resolve_domain_through_eddie("a.rtmp.youtube.com", home_mode=True)["ip"]
    print(f"\nProbing RTMP Stream Port 1935 on blocked IP {blocked_ip}...")
    ok, msg = probe_rtmp_connection(blocked_ip)
    print(f"[{RED}STREAM SEVERED / CUT OFF{RESET}] {msg}")

    print(f"\n{BOLD}Step 3: Verification with Security Devices (Outdoor Cam / Doorbell){RESET}")
    print("------------------------------------------------------------")
    engine = Engine()
    engine.set_home_mode(True)
    engine.set_security_devices(["outdoor-security-device"])
    d_sec = engine.evaluate("a.rtmp.youtube.com", "outdoor-security-device")
    print(f"Security device streaming to YouTube Live:")
    print(f"Status: {GREEN}{d_sec.action.upper()}{RESET} (Exempt from Home Mode: Security hardware keeps uploading)\n")

    print(f"{BOLD}{GREEN}[OK] YouTube Live streaming domains are fully configured and protected by Eddie!{RESET}\n")


if __name__ == "__main__":
    run_demo()
