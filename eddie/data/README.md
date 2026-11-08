# Hand-written domain labels

`eddie/labels.py` `DOMAINS` maps a domain suffix to `(company, category)`. The entries for the "What they know" report categories (#134) were written by hand on 2026-11-07, from each service's own public domain: the address its website, app or device is known to use. Sources by group:

- **kids** (YouTube Kids, PBS Kids, ABCmouse, Roblox), **music** (Spotify and its `scdn.co` CDN, Pandora, Apple Music), **health** (GoodRx, CVS, Walgreens, Teladoc, BetterHelp, MyFitnessPal), **finance** (Chase, Venmo, Affirm, Credit Karma), **shopping** (Target, Walmart, eBay), **work** (Slack, Zoom, Microsoft Teams), **jobs** (Indeed, Glassdoor, ZipRecruiter), **news** (The New York Times, Fox News, CNN, NPR), **religion** (Bible Gateway, Quran.com, Chabad.org) and **politics** (the Democratic and Republican parties' sites, Ballotpedia): the service's own registered website domain.
- **doorbell** (`skybell.com`) and **thermostat** (`ecobee.com`, Honeywell's Total Connect Comfort `tccna.honeywell.com`): the maker's own domain, which its app and cloud use.
- **assistant**: Amazon's Alexa Voice Service hosts (`avs-alexa-na.amazon.com`, `alexa.na.gateway.devices.a2z.com`) and Siri's `guzzoni.apple.com`, hosts commonly reported for these assistants in DNS logs.
- **id-sync** (in `TRACKING`): ID syncing domains of identity companies: LiveRamp (`rlcdn.com`), ID5 (`id5-sync.com`), Tapad (`tapad.com`) and The Trade Desk's Unified ID 2.0 (`uidapi.com`).

Known gaps: these were written from the contributors' knowledge of each service, and none was checked against a capture or a real device's lookups on this network (only the Fire TV entries came from one). The lists are short samples, not complete; a service that changes its domains or serves through a CDN under another name is missed and shows as `unknown`. Google Nest is left out because `nest.com` serves its cameras, doorbells and thermostats alike. A label is a domain lookup only: it says a service was contacted, never what was read, bought or said.

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

# GeoIP country database

`geoip.json` pins DB-IP's IP to Country Lite CSV (November 2026: URL, size and SHA-256) for geo alerts (#102). The file itself isn't committed: `scripts/provision_geoip.py` downloads it to `eddie/data/geoip/` (git- and docker-ignored) and the DNS server refuses it unless its hash matches the pin. License: CC BY 4.0, [IP Geolocation by DB-IP](https://db-ip.com). DB-IP only keeps recent monthly files online; to update, change `url`, `size` and `sha256` together and run the script again. Locations are approximate (CDNs, anycast, cloud regions).

# Country names

`countries.json` maps ISO 3166 alpha-2 codes to English names for alerts and speech. Generated 2026-11-07 from tzdata's `iso3166.tab` (public domain), with "&" spelled "and", "St" spelled "Saint", and a few names changed to how they're said aloud ("the United States", "the United Kingdom", "South Korea").

# Domain-name model

`domain_model.json` holds the classifier's weights (#181): a bias, two thresholds, about 39,000 feature weights and the build report in `meta`. `scripts/build_domain_model.py` built it on 2026-11-08 from the shipped `blocklists/*.txt` and `allowlist.txt` and the [Cisco Umbrella top-1M list](http://s3-us-west-1.amazonaws.com/umbrella-static/top-1m.csv.zip) (`top-1m.csv.zip`, downloaded 2026-11-08, SHA-256 `e1e7ef04ff50830009003e4dd5bf074601e635485c67c95fd8e103ac79b929ac`). Only learned weights are stored: no hostname list is shipped. The blocklists' own licenses apply to what the weights learned from them. Rebuild explicitly during setup; Eddie never downloads or trains it at runtime. How well it does, and how the threshold was audited, is in docs/ARCHITECTURE.md.
