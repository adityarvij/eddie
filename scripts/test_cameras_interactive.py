"""Interactive local tool to test renaming devices and checking Home Mode blocking."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eddie.decision import Engine, is_security_device, CAMERA_DOMAINS

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def test_device_name(name: str, home_mode_on: bool = True):
    engine = Engine()
    engine.set_home_mode(home_mode_on)

    # Check if the name auto-qualifies as a security device (doorbell, outdoor, alarm, etc.)
    is_sec = is_security_device(label=name, hostname=name)
    if is_sec:
        engine.set_security_devices(["test-device-mac"])

    decision = engine.evaluate("upload.ring.com", "test-device-mac")

    status = f"{RED}BLOCKED (Home Mode){RESET}" if decision.action == "block" else f"{GREEN}ALLOWED (Exempt/Active){RESET}"
    classification = f"{GREEN}Outside / Security Device{RESET}" if is_sec else f"{YELLOW}Inside Camera{RESET}"

    print(f"Device Name: {BOLD}{name:<25}{RESET}")
    print(f"  Classification:  {classification}")
    print(f"  Home Mode State: {'ON' if home_mode_on else 'OFF'}")
    print(f"  upload.ring.com: {status}\n")


def main():
    print(f"\n{BOLD}{CYAN}=== EDDIE CAMERA BLOCKING TEST TOOL ==={RESET}\n")
    print("Testing sample device names with Home Mode = ON:\n")

    samples = [
        "Living Room Cam",
        "Front Doorbell",
        "Outdoor Floodlight Cam",
        "Nursery Camera",
        "Backyard Ring Cam",
        "Bedroom Cam",
        "Garage Doorbell",
        "Office Cam",
    ]

    for s in samples:
        test_device_name(s, home_mode_on=True)

    print(f"{BOLD}Try entering your own device name (or press Enter to quit):{RESET}")
    while True:
        try:
            custom = input("Enter device name > ").strip()
            if not custom:
                break
            test_device_name(custom, home_mode_on=True)
        except (KeyboardInterrupt, EOFError):
            break

    print("\nDone!")


if __name__ == "__main__":
    main()
