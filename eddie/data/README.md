# Tracker ownership snapshot

`tracker_owners.json` is a small domain → display-name extract from [DuckDuckGo Tracker Radar](https://github.com/duckduckgo/tracker-radar/blob/main/build-data/generated/domain_map.json), downloaded 2026-11-07. Upstream SHA-256: `99beaa7201c7f0d668cf45b19ea6cb53c8cf2990a538e52d501cc7ae332dc53c`. Attribution: Copyright 2020 Duck Duck Go, Inc.; CC BY-NC-SA 4.0, see TRACKER_RADAR_LICENSE. This extract is under the same license. No DuckDuckGo marks are included.

It supplements the hand-written domain labels; ownership alone does not imply tracking. This is an offline snapshot, not a complete list or runtime lookup. Update explicitly during setup. The bundled Public Suffix List in tldextract handles unknown domains without a network fetch.

# MAC vendor table

`oui_vendors.json` maps a short household brand ("Amazon", "Apple", "Roku", ...) to its 24-bit IEEE OUI prefixes, so `/devices` can show "Amazon device" for an unlabeled Fire Stick. It was built by `scripts/build_oui.py` from Wireshark's `manuf` file (generated from the IEEE MA-L registry), downloaded 2026-11-07, SHA-256 `f09b092d9a2be8903e5614443021aaaf544fab745fb5b2b1ac20739f9af3233e`. Only the brands listed in the script are kept (8,133 prefixes); 28- and 36-bit blocks and every other vendor are left out and show as unknown. Phones' private (locally administered) addresses never match. Rebuild explicitly during setup; Eddie never fetches it at runtime.

# Data brokers and ad exchanges

`brokers.json` maps a tracker domain to `{company, kind}`, where `kind` is `data broker` or `ad exchange`. It was built by `scripts/build_brokers.py` from three public files, all downloaded 2026-11-07:

- the [California Data Broker Registry](https://cppa.ca.gov/data_broker_registry/) 2026 export (`registry.csv`, 625 registered brokers, a public government record), SHA-256 `9a465f1104a55eec20fd733d7f7fa395b10f87d8c974ef7f291e553a567b870d`;
- Tracker Radar's [`entity_map.json`](https://github.com/duckduckgo/tracker-radar/blob/main/build-data/generated/entity_map.json), SHA-256 `e3baf1cc37e5c820a74d0ce54e1d10d8404a11a7864632cdc693b5b36305b8fb`;
- DuckDuckGo's tracker data set (`extension-tds.json`), SHA-256 `fe110a0454448d92c9ca5f3cdca692d9b86bb56f86a9142389b8056e4fc02f3b`.

A company is included when its registry website belongs to a Tracker Radar entity (90 matched), and only that entity's domains DuckDuckGo lists as trackers are kept (61 domains, 43 companies), so a registered broker's own storefront, such as gm.com, is never highlighted. Registered brokers that run ad exchanges (OpenX, Magnite, Index Exchange, PubMatic, TripleLift, Sovrn, Sharethrough, Yieldmo, 33Across, Kargo; hand-named in the script) are marked `ad exchange`. The Tracker Radar parts carry the same CC BY-NC-SA 4.0 license as above.

Known gaps: exchanges run by companies that are not registered brokers (Google's doubleclick.net, Microsoft's adnxs.com) are not included; brokers whose website Tracker Radar doesn't know, or whose tracking domains it files under another entity (Index Exchange's casalemedia.com), are missed. Rebuild explicitly during setup; Eddie never fetches it at runtime.
