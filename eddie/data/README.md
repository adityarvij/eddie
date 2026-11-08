# Tracker ownership snapshot

`tracker_owners.json` is a small domain → display-name extract from [DuckDuckGo Tracker Radar](https://github.com/duckduckgo/tracker-radar/blob/main/build-data/generated/domain_map.json), downloaded 2026-11-07. Upstream SHA-256: `99beaa7201c7f0d668cf45b19ea6cb53c8cf2990a538e52d501cc7ae332dc53c`. Attribution: Copyright 2020 Duck Duck Go, Inc.; CC BY-NC-SA 4.0, see TRACKER_RADAR_LICENSE. This extract is under the same license. No DuckDuckGo marks are included.

It supplements the hand-written domain labels; ownership alone does not imply tracking. This is an offline snapshot, not a complete list or runtime lookup. Update explicitly during setup. The bundled Public Suffix List in tldextract handles unknown domains without a network fetch.

# MAC vendor table

`oui_vendors.json` maps a short household brand ("Amazon", "Apple", "Roku", ...) to its 24-bit IEEE OUI prefixes, so `/devices` can show "Amazon device" for an unlabeled Fire Stick. It was built by `scripts/build_oui.py` from Wireshark's `manuf` file (generated from the IEEE MA-L registry), downloaded 2026-11-07, SHA-256 `f09b092d9a2be8903e5614443021aaaf544fab745fb5b2b1ac20739f9af3233e`. Only the brands listed in the script are kept (8,133 prefixes); 28- and 36-bit blocks and every other vendor are left out and show as unknown. Phones' private (locally administered) addresses never match. Rebuild explicitly during setup; Eddie never fetches it at runtime.

# GeoIP country database

`geoip.json` pins DB-IP's IP to Country Lite CSV (November 2026: URL, size and SHA-256) for geo alerts (#102). The file itself isn't committed: `scripts/provision_geoip.py` downloads it to `eddie/data/geoip/` (git- and docker-ignored) and the DNS server refuses it unless its hash matches the pin. License: CC BY 4.0, [IP Geolocation by DB-IP](https://db-ip.com). DB-IP only keeps recent monthly files online; to update, change `url`, `size` and `sha256` together and run the script again. Locations are approximate (CDNs, anycast, cloud regions).

# Country names

`countries.json` maps ISO 3166 alpha-2 codes to English names for alerts and speech. Generated 2026-11-07 from tzdata's `iso3166.tab` (public domain), with "&" spelled "and", "St" spelled "Saint", and a few names changed to how they're said aloud ("the United States", "the United Kingdom", "South Korea").
