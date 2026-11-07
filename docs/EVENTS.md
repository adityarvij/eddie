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

- `snapshot`: sent once on connect: `{events, totals, home_mode}`
- `query`: the event above plus `id`, `company`, `category`, `beacon_s`, `idle`

| Added field | Notes |
|-------------|-------|
| `beacon_s`  | int or `null`. The interval in seconds when this device has been looking this domain up on a regular timer (last 6 to 12 lookups, gap coefficient of variation ≤ 0.15, 10 s to 1 h); `null` otherwise. Set whatever the action, so blocklisted beacons show too. |
| `idle`      | `true` when the device had no allowed media (streaming) lookup in the 5 minutes before this one, `false` when it did (or this is one), `null` while the device has never had one. |

Both are computed by the API from the events it has received, keyed by
`device_mac`, so senders don't send them. They restart with the API.
- `totals`: `{seen, blocked}`
- `mode`: `{home_mode: bool}`
- `rule_change`: a rule was stored or deleted (see Rules below)

## Home mode: `GET/POST /mode`

`POST /mode {"home_mode": true}`. The decision engine reads `GET /mode` (or
shares the state in-process later) and, while on, blocks a device's
non-essential traffic with `source: "home_mode"`.

## Rules: `GET/POST /rules`, `DELETE /rules/{id}`

Rules live in the `rules` table of `eddie.db` (the API opens `EDDIE_DB`, the
same file the DNS server uses; `EDDIE_DB=off` answers 503). They are checked
before every list: device rule, then global rule (docs/ARCHITECTURE.md).

`POST /rules`

```json
{"pattern": "device-metrics-us.amazon.com", "action": "block", "device_mac": "172.20.10.5", "created_by": "voice"}
```

| Field        | Notes |
|--------------|-------|
| `pattern`    | `a.com` matches only a.com; `*.a.com` matches a.com and every subdomain. Lowercased, trailing dot dropped. At least two labels: empty, `*`, `com` and `*.com` are refused. IP addresses are refused (`192.168.1.10`, `*.10.0.0.1`, `::1`): a name whose last label is all digits isn't a domain. |
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
   "queries": 412, "blocked": 160, "flagged": 12}
]}
```

| Field | Notes |
|-------|-------|
| `mac` | the device key used in `queries` and rules: its MAC, or its client IP if ARP never resolved it (Eddie itself, a client on another subnet) |
| `name` | first match wins: `label`, DHCP `hostname`, `"<vendor> device"`, then `mac` |
| `vendor` | from the MAC's OUI prefix (bundled table of household brands); null if unknown or the MAC is a phone's private (randomised) address |
| `hostname` | from a dnsmasq leases file (router mode); null on the hotspot. Never from reverse DNS |
| `queries` / `blocked` / `flagged` | lookups in the window: all, blocked by Eddie, forwarded while unknown |

Sorted most blocked first. A device's first lookups can be logged under its
IP before the first ARP read (about a second), so a short-lived IP row may
appear next to the MAC row.

`PUT /devices/{mac}` with `{"label": "Fire Stick"}` names a device Eddie has
seen (`404` otherwise); `""` or `null` clears it. Labels are at most 40
printable characters. Answers with the stored device and its `name`.

## What they know: `GET /what-they-know?hours=24`

`hours` is 1 to 168. Returns `{hours, facts, text}` where each fact is
`{fact, evidence, confidence}` computed in `eddie/inferences.py` from the
in-memory event log (tracking company exposure, active streaming hours,
after-midnight use, longest streaming gap, idle telemetry, and the device
and domain looked up most on a regular timer). `text` is a
template over those facts; the local-model phrasing and number check are not
built yet. Empty traffic returns no facts, never guesses.

## Local voice bridge (port 5173)

The dashboard bridge SSE endpoint `/events` emits `voice_status` with `status` (`off`, `starting`, `ready`, `error`), optional `engine` (`vosk` prototype or `openwakeword`) and `message`. `GET /api/voice/status` returns the latest status. Start/stop POST endpoints operate the local microphone. These are separate from the traffic API on port 8000.

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

The API retains at most 1,000 verdicts in memory, returns them from `GET /ai/verdicts`, and includes `ai_verdicts` in WebSocket snapshots so a browser refresh does not hide completed results. The fake generator uses these real cached verdicts for later decisions, while still labelling the traffic itself as scripted demo traffic in its launcher/docs. The old hard-coded “sent to AI” and fabricated AI confidence text are removed. Unknowns say “awaiting classification”; an `[ai]` line is printed only after actual model output is validated. These verdicts do not prove anything about real Fire Stick traffic.
