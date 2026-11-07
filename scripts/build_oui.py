"""Builds eddie/data/oui_vendors.json from Wireshark's manuf file (IEEE registry).

Setup-time only: Eddie never downloads vendor data at runtime. Fetch manuf
yourself (https://www.wireshark.org/download/automated/data/manuf), then:

    uv run python scripts/build_oui.py path/to/manuf

Only 24-bit (MA-L) prefixes of household brands are kept, under a short
display name, so an unlabeled Fire Stick reads "Amazon device" rather than
"Amazon Technologies Inc.". Everything else stays unknown.
"""
import json
import re
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "eddie" / "data" / "oui_vendors.json"

# Registry name (start, case-insensitive) -> display name.
VENDORS = [
    (r"amazon technologies", "Amazon"),
    (r"apple, inc", "Apple"),
    (r"google", "Google"),
    (r"nest labs", "Google"),
    (r"samsung electronics", "Samsung"),
    (r"roku", "Roku"),
    (r"raspberry pi", "Raspberry Pi"),
    (r"sonos", "Sonos"),
    (r"lg electronics", "LG"),
    (r"sony", "Sony"),
    (r"microsoft", "Microsoft"),
    (r"nintendo", "Nintendo"),
    (r"ring llc", "Ring"),
    (r"espressif", "Espressif"),
    (r"tuya", "Tuya"),
    (r"tp-link", "TP-Link"),
    (r"xiaomi", "Xiaomi"),
    (r"huawei", "Huawei"),
    (r"oneplus", "OnePlus"),
    (r"motorola mobility", "Motorola"),
    (r"vizio", "Vizio"),
    (r"tcl ", "TCL"),
    (r"hisense", "Hisense"),
    (r"philips lighting|signify", "Philips Hue"),
    (r"wyze", "Wyze"),
    (r"arlo", "Arlo"),
    (r"ecobee", "ecobee"),
    (r"intel corp", "Intel"),
    (r"dell inc", "Dell"),
    (r"hp inc|hewlett packard$", "HP"),
    (r"lenovo", "Lenovo"),
    (r"asustek", "ASUS"),
    (r"netgear", "Netgear"),
    (r"eero", "eero"),
    (r"belkin", "Belkin"),
    (r"irobot", "iRobot"),
    (r"garmin", "Garmin"),
    (r"fitbit", "Fitbit"),
    (r"peloton", "Peloton"),
    (r"canon inc", "Canon"),
    (r"seiko epson", "Epson"),
    (r"brother industries", "Brother"),
]


def build(manuf: str) -> dict[str, list[str]]:
    patterns = [(re.compile(p, re.I), name) for p, name in VENDORS]
    out: dict[str, list[str]] = {}
    for line in manuf.splitlines():
        cols = [c.strip() for c in line.split("\t")]
        if line.startswith("#") or len(cols) < 3 or len(cols[0]) != 8:  # "F0:4F:7C", 24-bit only
            continue
        name = next((n for p, n in patterns if p.match(cols[2])), None)
        if name:
            out.setdefault(name, []).append(cols[0].replace(":", "").lower())
    return {name: sorted(prefixes) for name, prefixes in sorted(out.items())}


if __name__ == "__main__":
    table = build(Path(sys.argv[1]).read_text(encoding="utf-8"))
    OUT.write_text(json.dumps(table, separators=(",", ":")) + "\n")
    print(f"{sum(map(len, table.values()))} prefixes, {len(table)} vendors -> {OUT}")
