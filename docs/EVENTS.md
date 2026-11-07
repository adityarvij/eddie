# Event contract (agree on this with Will before anything else)

The DNS server (Will) and the fake traffic generator (`scripts/fake_traffic.py`)
send **the exact same JSON** to the API. Swapping fake for real traffic means
pointing a different sender at the same endpoint. Nothing else changes.

## Sending: `POST /ingest`

Body is one event or a list of events.

```json
{
  "type": "query",
  "ts": 1762531200123,
  "device_mac": "f0:4f:7c:11:22:33",
  "device": "Fire Stick",
  "domain": "device-metrics-us.amazon.com",
  "qtype": "A",
  "action": "block",
  "source": "blocklist",
  "reason": "On the Amazon Fire TV tracker list"
}
```

| Field        | Type   | Notes |
|--------------|--------|-------|
| `type`       | string | always `"query"` |
| `ts`         | int    | milliseconds since epoch, when the query arrived |
| `device_mac` | string | MAC if known, otherwise `"ip:172.20.10.5"` |
| `device`     | string | friendly label (`"Fire Stick"`); fall back to the IP |
| `domain`     | string | lowercase, no trailing dot |
| `qtype`      | string | `A`, `AAAA`, `HTTPS`, ... |
| `action`     | string | `allow` \| `block` \| `flag` |
| `source`     | string | `rule` \| `allowlist` \| `blocklist` \| `heuristic` \| `ai` \| `home_mode` \| `default` |
| `reason`     | string | one short human line, shown on screen |

The DNS server sends every lookup here through `eddie/feed.py`: fire-and-forget
from a background thread, in batches (a list body), with a 1 s timeout, so DNS
never waits. `EDDIE_API_URL` (default `http://127.0.0.1:8000`, local addresses
only) picks the API and `EDDIE_FEED=off` turns it off. Events the API doesn't
accept in time are dropped, not retried. Real events carry the device's MAC
and name (see Devices below); a client whose MAC isn't known yet is
`device_mac: "ip:<client IP>"` and `device: "<client IP>"`. A new label shows
in the feed within 30 s.

The DNS server doesn't send every lookup. Network plumbing (reverse lookups
under `.arpa`, service-discovery names with a `_` label such as
`_ipp._tcp.…`, `.local` and single-label names) is never sent. The same
device, domain and action again within 2 s is sent once, so a browser asking
for A and AAAA together is one event, and `qtype` is the first one asked.
Both are still answered and written to `eddie.db`. So `totals.seen`,
`/summary` and the spoken summary count feed events, not every DNS packet.

## Receiving: `WS /ws/events`

The API adds `id`, `company` and `category` (from `eddie/labels.py`), plus
`beacon_s` and `idle` (from `eddie/beacons.py`, #24), and pushes:

- `snapshot`: sent once on connect: `{events, ai_verdicts, bypass, totals, home_mode}`
- `query`: the event above plus `id`, `company`, `category`, `beacon_s`, `idle`

| Added field | Notes |
|-------------|-------|
| `beacon_s`  | int or `null`. The interval in seconds when this device has been looking this domain up on a regular timer (last 6 to 12 lookups, gap coefficient of variation ≤ 0.15, 10 s to 1 h); `null` otherwise. Set whatever the action, so blocklisted beacons show too. |
| `idle`      | `true` when the device had no allowed media (streaming) lookup in the 5 minutes before this one, `false` when it did (or this is one), `null` while the device has never had one. |

Both are computed by the API from the events it has received, keyed by
`device_mac`, so senders don't send them. They restart with the API.
- `totals`: `{seen, blocked}`
- `mode`: `{home_mode: bool}`
- `offline_mode`: `{offline: bool, applied: bool}` after every `POST /offline-mode`
- `rule_change`: a rule was stored or deleted (see Rules below)
- `bypass_suspected`: a device looked up an encrypted DNS resolver (see Bypass detection below)

## Bypass detection: `bypass_suspected` (#97)

For each query event whose `domain` is, or is under, a name in
`eddie/bypass.py` `RESOLVERS` (`dns.google`, `cloudflare-dns.com`,
`dns.quad9.net`, `dns.nextdns.io`, …; sources listed in the module), the API
pushes, right after the query:

```json
{"type": "bypass_suspected", "ts": 1762531200123, "device_mac": "f0:4f:7c:11:22:33", "device": "Fire Stick",
 "domain": "dns.google", "resolver": "Google Public DNS", "blocked": false,
 "reason": "Fire Stick looked up dns.google (Google Public DNS), an encrypted DNS resolver. It may be sending some lookups there, where Eddie can't see or block them."}
```

- At most once per `device_mac` and resolver name every 10 minutes.
- `blocked` is the query's own `action == "block"`, i.e. the DNS server's
  decision for that lookup (with `EDDIE_BLOCK_DOH=on`, or a rule or list).
  Its `reason` then adds that blocking the name only stops encrypted DNS set
  up by name, not an app that has the resolver's IP.
- It's a suspicion: a browser may check a resolver and not use it, and a
  device with hard-coded resolver IPs never asks Eddie, so no event (or
  absence of one) means a device is fully covered by Eddie.
- Not a lookup: it doesn't change `totals`. The last 50 are kept in memory
  and sent as `bypass` in the `snapshot`; a restart clears them.
- The dashboard shows it as a `[bypass?]` feed row and a toast with `reason`.

## Home mode: `GET/POST /mode`

`POST /mode {"home_mode": true}`. The decision engine reads `GET /mode` (or
shares the state in-process later) and, while on, blocks a device's
non-essential traffic with `source: "home_mode"`.

`GET /mode` also returns `offline` and `offline_applied` (below).

## Offline mode: `POST /offline-mode` (#26)

`POST /offline-mode {"offline": true}` stores the state in `engine_state`
(`offline`, `offline_rev`) and waits up to 2 s for the DNS server to load it,
the same handshake as rules. It answers `{"offline": true, "applied": true}`
only once DNS has written `offline_loaded` ≥ that revision; `applied: false`
means it's stored but no running DNS server confirmed it (DNS loads it when it
starts). `GET /mode` reports `offline` (the stored state) and
`offline_applied`. With `EDDIE_DB=off` the POST answers 503 and `GET /mode`
reports `offline: false, offline_applied: false`.

While offline the DNS server forwards nothing. Blocked lookups are answered as
usual; an allowed lookup gets the answer it last got from upstream (kept in
memory, at most 4,096 answers and 24 h old, TTLs aged, 30 s once expired), or
SERVFAIL. Query rows are logged with the normal decision. The state survives
restarts of both processes, so it stays on until it's turned off.

## Rules: `GET/POST /rules`, `DELETE /rules/{id}`

Rules live in the `rules` table of `eddie.db` (the API opens `EDDIE_DB`, the
same file the DNS server uses; `EDDIE_DB=off` answers 503). They are checked
before every list: device rule, then the device's lockdown rule, then global rule (docs/ARCHITECTURE.md).

`POST /rules`

```json
{"pattern": "device-metrics-us.amazon.com", "action": "block", "device_mac": "172.20.10.5", "created_by": "voice"}
```

| Field        | Notes |
|--------------|-------|
| `pattern`    | `a.com` matches only a.com; `*.a.com` matches a.com and every subdomain. Lowercased, trailing dot dropped. At least two labels: empty, `com` and `*.com` are refused. The one exception is `*` with `action: block` and a `device_mac`: lockdown (#17), which blocks every domain for that device except its own device rules and the allowlist. `*` without a device or as an allow is refused. IP addresses are refused (`192.168.1.10`, `*.10.0.0.1`, `::1`): a name whose last label is all digits isn't a domain. |
| `action`     | `block` \| `allow` |
| `device_mac` | optional; omit for every device. Use the device's `mac` from `GET /devices` (its MAC, or its client IP while the MAC is unknown). A rule made with a client IP also matches the device after its MAC becomes known. |
| `created_by` | optional, default `"user"`; exactly `user` \| `voice` \| `agent` |

Answers `201` with the stored row plus the DNS acknowledgment:

```json
{"id": 7, "scope": "device", "device_mac": "172.20.10.5", "pattern": "device-metrics-us.amazon.com",
 "action": "block", "created_by": "voice", "ts": 1762531200123, "applied": true}
```

- `id` is the stored rule. Keep it: undo is `DELETE /rules/{id}`.
- `applied: true` means the running DNS server has loaded this change, so the
  next lookup Eddie answers uses it. The API waits up to 2 s for that.
- `applied: false` means the rule is stored but no DNS server loaded it in
  time. **Do not tell the user it is blocked.** It takes effect if and when
  a DNS server on the same `eddie.db` loads it.
- Bad input answers `422` and stores nothing.

`DELETE /rules/{id}` answers `200` with the removed row plus `"deleted": true`
and `applied` (same meaning: the previous decision is back once it's true),
or `404` if there is no such rule.

`GET /rules` lists every rule, newest first, without `applied`. Each row also
has `device`: the device's display name (label, DHCP hostname,
`"<vendor> device"`, or its key if Eddie never recorded it), or `null` for a
rule on every device.

Every successful add or delete also pushes a `rule_change` on `/ws/events`:

```json
{"type": "rule_change", "change": "added", "id": 7, "scope": "device", "device_mac": "172.20.10.5",
 "device": "Fire Stick", "pattern": "device-metrics-us.amazon.com", "action": "block", "created_by": "voice",
 "ts": 1762531200123}
```

- `change` is `added` or `removed`; the rest is the stored row (`ts` is when
  the rule was created, also for `removed`) plus `device`, named as in
  `GET /rules`.
- It is sent as soon as the row is written or deleted, **before** the API waits
  for DNS. It means "stored" only. It has no `applied` field and never says the
  rule is enforced; only the HTTP answer's `applied: true` says that.
- Refused requests (`422`) and `404` deletes send nothing. Clients that
  connect later don't get past changes; the snapshot doesn't include rules, so
  read `GET /rules` for the current list.
- It carries `pattern`, not `domain`, so a feed that renders anything with a
  `domain` as a lookup won't show it as one.
- The dashboard (#19) shows it as a `[rule]` feed row and a toast, and
  re-reads the list through `serve.py`'s read-only `GET /api/rules`.

Devices cache answers (60 s for blocked lookups, the upstream TTL for allowed
ones), so a device may not ask Eddie again until its cache expires.

## Devices: `GET /devices`, `PUT /devices/{mac}`

The DNS server maps each client IP to a MAC from the ARP table
(`eddie/devices.py`) and records every device that looks something up in the
`devices` table of `eddie.db`. Lookup counts come from the DNS query log, so
this needs `EDDIE_DB` (off answers 503).

`GET /devices?hours=24` (1 to 168):

```json
{"hours": 24, "devices": [
  {"mac": "f0:4f:7c:11:22:33", "ip": "172.20.10.5", "name": "Fire Stick", "label": "Fire Stick",
   "hostname": null, "vendor": "Amazon", "first_seen": 1762531200123, "last_seen": 1762534800456,
   "queries": 412, "blocked": 160, "flagged": 12, "trackers": 14,
   "recent_blocks": [{"domain": "device-metrics-us.amazon.com", "ts": 1762534800456},
                     {"domain": "unagi-na.amazon.com", "ts": 1762534790001}]}
]}
```

| Field | Notes |
|-------|-------|
| `mac` | the device key used in `queries` and rules: its MAC, or its client IP if ARP never resolved it (Eddie itself, a client on another subnet) |
| `name` | first match wins: `label`, DHCP `hostname`, `"<vendor> device"`, then `mac` |
| `vendor` | from the MAC's OUI prefix (bundled table of household brands); null if unknown or the MAC is a phone's private (randomised) address |
| `hostname` | from a dnsmasq leases file (router mode); null on the hotspot. Never from reverse DNS |
| `queries` / `blocked` / `flagged` | lookups in the window: all, blocked by Eddie, forwarded while unknown |
| `trackers` | distinct domains blocked in the window (each counted once however often it was asked for) |
| `recent_blocks` | up to three of those domains, newest last block first, with that block's `ts` |

Sorted most blocked first. A device's first lookups can be logged under its
IP before the first ARP read (about a second), so a short-lived IP row may
appear next to the MAC row.

`PUT /devices/{mac}` with `{"label": "Fire Stick"}` names a device Eddie has
seen (`404` otherwise); `""` or `null` clears it. Labels are at most 40
printable characters. Answers with the stored device and its `name`.

## What they know: `GET /what-they-know?hours=24`

`hours` is 1 to 168. Returns `{demo: false, source, hours, facts, text}`.
`source` is `"eddie.db"` when the DNS server's query log (`EDDIE_DB`, read-only)
has lookups in the window, otherwise `"feed"`: the API's in-memory event list
(at most 20,000 events, lost on restart), which is what `fake_traffic.py` fills.
The two are never mixed. Database rows are named by device label, else
"<vendor> device", else "unlabeled device" (all such devices share that name);
never by MAC, IP or DHCP hostname. Each fact is `{fact, evidence, confidence}`
computed in `eddie/inferences.py` (tracking company exposure, active streaming
hours, after-midnight streaming sessions, longest streaming gap, idle telemetry,
category by weekday, and the device
and domain looked up most on a regular timer). `text` is a
template over those facts; voice has the local model rephrase the facts
(`voice/ollama_agent.py`) and falls back to `text`. Empty traffic returns no
facts, never guesses.

Sessions group requests of the same category with every gap under 10 minutes
(`inferences.sessions`). Weekday facts need sessions on at least two calendar
days: the weekday with the most session minutes per human category, and
"heavier on weekends/weekdays" when one side's average daily minutes is at
least 1.5 times the other's. A session counts at least one minute.

`?demo=true` ignores `hours` and returns `{demo: true, label, hours: 168,
facts, text}` computed from `eddie/demo_week.py`: an invented, seeded week
(devices named "Living room TV (demo)" and "Kids' tablet (demo)", every event
`demo: true`). `text` starts with `label`. Demo events are generated per
request and never enter `/events`, the WebSocket feed or `eddie.db`. The
voice tools only read the live report; "what do they know about me" asks for
`hours=168`, and the dashboard's live panel does too.

## Local voice bridge (port 5173)

The dashboard bridge SSE endpoint `/events` emits `voice_status` with `status` (`off`, `starting`, `ready`, `error`), optional `engine` (`vosk` prototype or `openwakeword`) and `message`. `GET /api/voice/status` returns the latest status. Start/stop POST endpoints operate the local microphone. These are separate from the traffic API on port 8000.

Relays to the traffic API (`EDDIE_API_URL`, default `http://127.0.0.1:8000`), so the page needs no CORS or direct API access. Each answers 502 `{message}` if the API errors or doesn't answer, never an empty or guessed result:

| Bridge | Traffic API | Notes |
|--------|-------------|-------|
| `GET /api/rules` | `GET /rules` | read-only |
| `GET /api/devices` | `GET /devices?hours=24` | read-only |
| `GET /api/mode` | `GET /mode` | read-only |
| `GET /api/what-they-know[?demo=1]` | `GET /what-they-know?hours=168` or `?demo=true` | read-only |
| `POST /api/mode` `{home_mode: bool}` | `POST /mode` | answers with the API's echo `{home_mode}` |
| `POST /api/offline-mode` `{offline: bool}` | `POST /offline-mode` | answers with `{offline, applied}`; `applied: false` is passed through |

The two POSTs forward only that one boolean (anything else is 400) and wait up to 5 s for the API.

Control auth (#40): POSTs need a `Host` that is an IP, `localhost` or `.local`, and a same-origin `Origin` if one is sent (403 otherwise). From the Eddie computer (loopback) no token is needed. From another device, only `POST /api/voice/ask` (`{text}`), `POST /api/mode` and `POST /api/offline-mode` are allowed, with `Authorization: Bearer <pairing token>`; a missing or wrong token gets 401 `{message}`. `/api/voice/start`, `/api/voice/stop`, `/api/voice/dismiss` and `/api/events` answer 403 from other devices whatever the token.

Wake lifecycle: `wake`, `voice_state: listening`, `voice_state: thinking`, optional `transcript`, `agent_reply`, `speaking`, `speech_end`, then `voice_state: listening` again for a follow-up without the wake phrase. If no speech starts within 4 seconds of listening, the worker emits `voice_state: dismissed` and the dashboard closes Eddie and returns to the live feed; the next request needs the wake phrase. No transcript is persisted. Capture or inference failure yields `voice_status: error`.


Voice recovery: after an initial wake with no command, or an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again. Four seconds of silence during a follow-up dismisses Eddie back to the newest feed activity. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.

Active conversation lifecycle: listening state events include `is_eddie_active: true`; after four seconds with no user speech, `voice_state: dismissed` includes `is_eddie_active: false`. This applies immediately after the wake and after every reply. Two empty transcriptions in a row also end with `dismissed`. `POST /api/voice/dismiss` (the overlay's close button; localhost only, `{"ok": true}`) ends an active conversation: the worker emits `voice_state: dismissed` instead of `listening` and waits for the wake phrase again. It also cancels any pending confirmation.

Confirmation: after Eddie reads back a risky action, the bridge emits `voice_state: confirming` with `kind`, `prompt` and `expires_in_ms` in place of `idle`; the worker's following `listening` event does not clear it. The next `thinking`, `dismissed` or the expiry clears it. Action arguments are never sent. The dashboard shows `prompt` (Eddie's read-back, not the user's words) under Eddie with “say yes to confirm or no to cancel” until then (#19).

## Company counts (#25)

`GET /summary` adds `companies` (top ten `{company, requests, blocked, tracking_requests}` rows) and `company_window_seconds: 3600`. Counts include only timestamps in the past hour, exclude future events, and use the same in-memory event window as the spoken summary. A request is a DNS lookup, not evidence that data was uploaded. Unknown owners fall back to a registrable domain using the bundled Public Suffix List, including private suffixes.

## Per-device counts and event filters (#29)

`GET /summary?window=15m|1h|24h` (default `1h`; anything else gets 422) adds `window` and `devices`: one row per device label seen in that window, `{device, requests, blocked, flagged, blocked_companies}`, busiest first, where `blocked_companies` is up to three `{company, blocked}` rows. `text` and `companies` still cover the last hour. Like the rest of `/summary`, counts come from the API's in-memory feed (at most 20,000 events), so a busy 24-hour window can be partial.

`GET /events` also takes `action=allow|block|flag` (anything else gets 422) and `domain=` (exact, already-normalized match). The voice agent uses these for recent blocks and domain explanations.


## Classifier verdicts (#27)

`POST /ai/verdict` takes `{domain, label, confidence, reason, ts}`. Label is `tracker|needed|unsure`, confidence is a finite number in [0,1], reason is printable, at most 14 words and 120 characters, and ts is epoch milliseconds. Unknown fields and invalid values get 422. A successful response is `{ok: true}` and broadcasts `{type: "ai_verdict", ...payload}` to `/ws/events`. This event does not change historical queries, counters, or rules; later DNS decisions use the cached verdict. Like `/ingest`, this existing local API has no authentication and should stay loopback-only. The classifier refuses external API URLs and redirects. The outbox is bounded to 1,000 in-memory events, retried without repeating inference; it is not a durable notification log.


## Real AI on the scripted demo feed

`python -m eddie.feed_classifier` polls the local API for scripted demo queries and feeds the real bounded Ollama worker using an in-memory SQLite queue/cache. Real DNS still uses the durable database worker. `run_all.sh` launches the adapter only with the fake-traffic profile. Stopping it loses its cache, and no demo network history is added to disk. It polls up to 2,000 recent events once per second and retains at most 20,000 query rows for an hour; heavy traffic can outrun that window. Inference remains on the host with local-only URLs, not in the DNS or API request path.

The API retains at most 1,000 verdicts in memory, returns them from `GET /ai/verdicts`, and includes `ai_verdicts` in WebSocket snapshots so a browser refresh does not hide completed results. The fake generator uses these real cached verdicts for later decisions, while still labelling the traffic itself as scripted demo traffic in its launcher/docs. The old hard-coded “sent to AI” and fabricated AI confidence text are removed. Unknowns say “awaiting classification”; an AI line (`[approved (ai)]`, `[unsure (ai)]` or `[flagged (ai)]`, coloured like that DNS result and stamped with the verdict time) is printed only after actual model output is validated; snapshot verdicts are shown among the DNS lines in time order. A tracker reads flagged, never blocked, because the AI does not block. These verdicts do not prove anything about real Fire Stick traffic.
