"""Builds eddie/data/brokers.json: tracker domains run by registered data brokers.

Setup-time only: Eddie never downloads this at runtime. Fetch the three
inputs yourself, then:

    uv run python scripts/build_brokers.py registry.csv entity_map.json tds.json

- registry.csv: California Data Broker Registry
  (https://cppa.ca.gov/data_broker_registry/registry.csv)
- entity_map.json: DuckDuckGo Tracker Radar
  (https://raw.githubusercontent.com/duckduckgo/tracker-radar/main/build-data/generated/entity_map.json)
- tds.json: DuckDuckGo tracker data set
  (https://staticcdn.duckduckgo.com/trackerblocking/v6/current/extension-tds.json)

A company is kept when its registry website belongs to a Tracker Radar
entity; only that entity's domains DuckDuckGo lists as trackers are written,
so a broker's own storefront (gm.com, t-mobile.com) is never highlighted.
Registered brokers that run ad exchanges are marked "ad exchange".
"""
import csv
import json
import sys
from pathlib import Path

import tldextract

OUT = Path(__file__).resolve().parents[1] / "eddie" / "data" / "brokers.json"

# Tracker Radar entity names of registered brokers that operate an ad exchange (SSP).
EXCHANGES = {
    "OpenX Technologies Inc", "Magnite, Inc.", "Index Exchange Limited", "PubMatic, Inc.",
    "TripleLift", "Sovrn Holdings", "Sharethrough, Inc.", "YieldMo, Inc.", "33Across, Inc.",
    "Kargo Global, Inc.",
}


def main(registry_path: str, entity_path: str, tds_path: str) -> None:
    extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)
    entities = json.loads(Path(entity_path).read_text())
    trackers = json.loads(Path(tds_path).read_text())["trackers"]
    owner_of = {domain: name for name, entity in entities.items() for domain in entity.get("properties", [])}
    with open(registry_path, encoding="utf-8-sig", errors="replace") as f:
        sites = [row["Data broker primary website:"].strip() for row in csv.DictReader(f)]
    brokers = set()
    for site in sites:
        host = site.split("://")[-1].split("/")[0]
        domain = extract(host).top_domain_under_public_suffix
        if domain in owner_of:
            brokers.add(owner_of[domain])
    out = {}
    for name in sorted(brokers):
        entity = entities[name]
        for domain in sorted(entity["properties"]):
            if domain in trackers:
                out[domain] = {"company": entity.get("displayName") or name,
                               "kind": "ad exchange" if name in EXCHANGES else "data broker"}
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"{len(brokers)} registered companies, {len(out)} tracker domains -> {OUT}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
