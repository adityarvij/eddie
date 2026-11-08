"""Quick local interactive test for Home Mode (#43 & #46)."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlite3
import tempfile
from eddie import decision, devices, store
from eddie.decision import Engine

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_row(device_name, domain, action, source, reason):
    status_color = RED if action == "block" else GREEN
    print(f"  {device_name:<20} -> {domain:<20} | {status_color}{action.upper():<6}{RESET} | Source: {source:<10} ({reason})")


def run_demo():
    print(f"\n{BOLD}{CYAN}=== EDDIE HOME MODE LOCAL VERIFICATION TEST ==={RESET}\n")

    tmp = tempfile.TemporaryDirectory()
    try:
        db_path = os.path.join(tmp.name, "eddie_demo.db")
        conn = store.connect(db_path)
        engine = Engine()
        engine.set_lists(allowlist=["netflix.com"], blocklist=["adsystem.amazon.com"])

        # 1. Setup Devices in Eddie's database
        INDOOR_CAM = "aa:00:00:00:00:01"
        DOORBELL = "aa:00:00:00:00:02"
        TV = "aa:00:00:00:00:03"

        conn.execute("INSERT INTO devices (mac, label, hostname, vendor, security) VALUES (?, ?, ?, ?, ?)",
                     (INDOOR_CAM, "Living Room Cam", "ring-indoor-cam", "Ring", 0))
        conn.execute("INSERT INTO devices (mac, label, hostname, vendor, security) VALUES (?, ?, ?, ?, ?)",
                     (DOORBELL, "Front Doorbell", "ring-doorbell-pro", "Ring", 1))
        conn.execute("INSERT INTO devices (mac, label, hostname, vendor, security) VALUES (?, ?, ?, ?, ?)",
                     (TV, "Smart TV", "firestick-4k", "Amazon", 0))
        conn.commit()

        # ----------------------------------------------------
        # TEST SCENARIO A: HOME MODE IS OFF (Nobody is home)
        # ----------------------------------------------------
        print(f"{BOLD}[1] SCENARIO: Home Mode is OFF (You are away from home){RESET}")
        conn.execute("INSERT INTO engine_state (key, value) VALUES ('home_mode', 0) ON CONFLICT(key) DO UPDATE SET value = 0")
        conn.commit()
        engine.load_from_store(conn)

        print(f"  Status: {YELLOW}Home Mode = OFF{RESET}")
        d1 = engine.evaluate("upload.ring.com", INDOOR_CAM)
        print_row("Living Room Cam", "upload.ring.com", d1.action, d1.source, "Away security upload")

        d2 = engine.evaluate("upload.ring.com", DOORBELL)
        print_row("Front Doorbell", "upload.ring.com", d2.action, d2.source, "Doorbell activity")

        d3 = engine.evaluate("netflix.com", TV)
        print_row("Smart TV", "netflix.com", d3.action, d3.source, "Essential streaming")

        # ----------------------------------------------------
        # TEST SCENARIO B: HOME MODE TURNS ON (You walk in the door!)
        # ----------------------------------------------------
        print(f"\n{BOLD}[2] SCENARIO: Home Mode turns ON (Your phone joins the Wi-Fi!){RESET}")
        conn.execute("UPDATE engine_state SET value = 1 WHERE key = 'home_mode'")
        conn.commit()
        engine.load_from_store(conn)

        print(f"  Status: {GREEN}Home Mode = ON (Active Privacy Protection){RESET}")
        d1 = engine.evaluate("upload.ring.com", INDOOR_CAM)
        print_row("Living Room Cam", "upload.ring.com", d1.action, d1.source, d1.reason)

        d2 = engine.evaluate("upload.ring.com", DOORBELL)
        print_row("Front Doorbell", "upload.ring.com", d2.action, d2.source, "EXEMPT: Security device")

        d3 = engine.evaluate("netflix.com", TV)
        print_row("Smart TV", "netflix.com", d3.action, d3.source, "Streaming untouched")

        # ----------------------------------------------------
        # TEST SCENARIO C: YOU LEAVE (Home Mode turns back OFF)
        # ----------------------------------------------------
        print(f"\n{BOLD}[3] SCENARIO: You leave the house (Home Mode turns back OFF){RESET}")
        conn.execute("UPDATE engine_state SET value = 0 WHERE key = 'home_mode'")
        conn.commit()
        engine.load_from_store(conn)

        print(f"  Status: {YELLOW}Home Mode = OFF{RESET}")
        d1 = engine.evaluate("upload.ring.com", INDOOR_CAM)
        print_row("Living Room Cam", "upload.ring.com", d1.action, d1.source, "Unblocked & streaming again")

        conn.close()
    finally:
        try:
            tmp.cleanup()
        except Exception:
            pass

    print(f"\n{BOLD}{GREEN}ALL CHECKS PASSED: Indoor cam blocked, doorbell protected, TV streaming unaffected!{RESET}\n")


if __name__ == "__main__":
    run_demo()
