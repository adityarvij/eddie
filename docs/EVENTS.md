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
| `action`     | string | `allow` \| `block` \| `flag` \| `spoof` (answered with Eddie's IP, #99) |
| `source`     | string | `rule` \| `allowlist` \| `blocklist` \| `heuristic` \| `ai` \| `home_mode` \| `away_mode` \| `sensitivity` \| `quarantine` \| `default` |
| `reason`     | string | one short human line, shown on screen |

The DNS server sends every lookup here through `eddie/feed.py`: fire-and-forget
from a background thread, in batches (a list body), with a 1 s timeout, so DNS
never waits. `EDDIE_API_URL` (default `http://127.0.0.1:8000`, local addresses
only) picks the API and `EDDIE_FEED=off` turns it off. Events the API doesn't
accept in time are dropped, not retried. Real events carry the device's MAC
and name (see Devices below); a client whose MAC isn't known yet is
`device_mac: "ip:<client IP>"` and `device: "<client IP>"`. A new label shows
in the feed within 30 s.

### Access (#96)

Writes (`POST`, `PUT`, `DELETE`) from loopback need nothing. From any other
address they need `Authorization: Bearer <EDDIE_API_TOKEN>`; a missing or wrong
token, or no token configured on the API, gets 401 `{detail}`. Every client
here (`eddie/feed.py`, the classifier, `voice/voice_agent.py`, the dashboard
bridge, `scripts/fake_traffic.py`) sends it when `EDDIE_API_TOKEN` is set. A
write whose `Origin` isn't a loopback page or a page on the same IP or `.local`
host gets 403. `GET`s are open. `/ws/events` refuses the handshake (403) for
the same foreign origins; non-browser clients send no `Origin` and connect.

The DNS server doesn't send every lookup. Network plumbing (reverse lookups
under `.arpa`, service-discovery names with a `_` label such as
`_ipp._tcp.…`, `.local` and single-label names) is never sent. The same
device, domain and action again within 2 s is sent once, so a browser asking
for A and AAAA together is one event, and `qtype` is the first one asked.
Both are still answered and written to `eddie.db`. So `totals.seen`,
`/summary` and the spoken summary count feed events, not every DNS packet.

## Receiving: `WS /ws/events`

The API adds `id`, `company`, `category` and `broker` (from `eddie/labels.py`), plus
`beacon_s` and `idle` (from `eddie/beacons.py`, #24), and pushes:

- `snapshot`: sent once on connect: `{events, ai_verdicts, bypass, geo, acr, questions, totals, home_mode}`
- `query`: the event above plus `id`, `company`, `category`, `broker`, `beacon_s`, `idle`; `broker` is `"data broker"`, `"ad exchange"` or `null` (see Data broker watch below)

| Added field | Notes |
|-------------|-------|
| `beacon_s`  | int or `null`. The interval in seconds when this device has been looking this domain up on a regular timer (last 6 to 12 lookups, gap coefficient of variation ≤ 0.15, 10 s to 1 h); `null` otherwise. Set whatever the action, so blocklisted beacons show too. |
| `idle`      | `true` when the device had no allowed media (streaming) lookup in the 5 minutes before this one, `false` when it did (or this is one), `null` while the device has never had one. |

Both are computed by the API from the events it has received, keyed by
`device_mac`, so senders don't send them. They restart with the API.
- `totals`: `{seen, blocked}`
- `mode`: `{home_mode, home_manual, home_applied, reason}` after every home mode change, sent once the API has waited for DNS to load it; `reason` is `manual` (`POST /mode`, the dashboard or voice) or `presence` (household phones, #20)
- `offline_mode`: `{offline: bool, applied: bool}` after every `POST /offline-mode`
- `away_mode`: `{away, away_until, away_applied, away_devices, away_exempt}` after every `POST /mode` with `away` (below)
- `sensitivity`: `{mode, on, protected, applied, sites, trackers, examples}` after every `POST /sensitivity` (see Sensitivity modes below)
- `rule_change`: a rule was stored or deleted (see Rules below)
- `question` / `question_resolved`: Eddie is asking whether to block a likely tracker, and its answer (see Ask before blocking below)
- `new_device`, `quarantine`, `device_status`: see New devices and quarantine below
- `bypass_suspected`: a device looked up an encrypted DNS resolver (see Bypass detection below)
- `geo_alert`: a device's DNS answers pointed to a country it hadn't used (see Geo alerts below)
- `acr_detected`: the DNS server blocked a smart TV's content recognition server it was checking in with on a timer (see ACR blocker below)
- `rule_rolled_back`: Eddie deleted a new block rule that looked like it broke a device (see Self-correcting blocks below)

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

## Geo alerts: `geo_alert`, `POST /geo/alerts` (#102)

The DNS server's geoip worker (`eddie/geoip.py`) looks up the public A and
AAAA addresses in each forwarded answer in the pinned offline country
database. Once a device has finished learning (50 answers *and* an hour since
its first), an address in a country it hasn't used before is posted to
`POST /geo/alerts` (one event or a list; a write, so it needs the API token
off loopback), and the API pushes it unchanged:

```json
{"type": "geo_alert", "ts": 1762531200123, "device_mac": "f0:4f:7c:11:22:33", "device": "Doorbell",
 "domain": "telemetry.example", "ip": "77.88.8.8", "country": "RU", "country_name": "Russia",
 "previous": ["US"], "approximate": true,
 "reason": "Doorbell started talking to a server in Russia (telemetry.example, 77.88.8.8). Until now its servers were in the United States. IP location is approximate: CDNs and cloud providers serve from many countries, so this is where the server's address is registered, not proof of where the data goes."}
```

- `domain` is the name the device looked up (a CNAME's target isn't named);
  `ip` is the address in the new country; `previous` is the ISO codes the
  device had used, sorted. `approximate` is always `true` and `reason`
  always carries the caveat: say it whenever an alert is read aloud or shown.
- Once per device and country. A device that raises more than 5 new countries
  in 24 hours stops raising them (a phone or laptop roaming the web).
- What a device has used lives in the DNS server's memory and restarts with
  it, so a restart means another learning period with no alerts.
- Blocked, spoofed and offline-cached answers aren't looked up.
- Not a lookup: it doesn't change `totals`. The last 50 are kept in memory
  and sent as `geo` in the `snapshot`; an API restart clears them.
- The dashboard shows it as a `[new country?]` feed row and a toast with `reason`.

## ACR blocker: `acr_detected` (#104)

For each query event whose `domain` is, or is under, a name in `eddie/acr.py`
`ACR` (`samsungacr.com`, `alphonso.tv`, `tvinteractive.tv`, `samba.tv`;
sources listed in the module), with `action == "block"` and a `beacon_s`, the
API pushes, right after the query:

```json
{"type": "acr_detected", "ts": 1762531200123, "device_mac": "f0:4f:7c:11:22:33", "device": "Living Room TV",
 "domain": "acr.samsungacr.com", "acr_name": "samsungacr.com", "provider": "Samsung",
 "interval_s": 15, "lookups": 6, "source": "heuristic",
 "reason": "Living Room TV was checking in with Samsung's content recognition (ACR) server acr.samsungacr.com every 15 seconds. ACR is how TVs report what's on screen. Blocked."}
```

- `interval_s` is the query's `beacon_s`: the mean gap the API measured over
  the last `lookups` lookups (6 to 12). It is never estimated; with no
  measured timer there is no event.
- Only for a lookup the DNS server blocked (`source` says why: `heuristic`
  for the ACR step in decision.py, or `blocklist`, `rule`, …). A user allow
  rule or the allowlist means no block and no event.
- At most once per `device_mac` and `domain` an hour.
- Not a lookup: it doesn't change `totals`. The last 20 are kept in memory and
  sent as `acr` in the `snapshot`; a restart clears them.
- The dashboard shows an `[acr blocked]` feed row, a toast with `reason`, and
  the "acr blocked" card with the latest one per device and domain.

## Device baselines: `new_domain`, `baseline_state`, `/baselines`

The API keeps each device's usual registrable domains in `eddie.db`
(`eddie/baselines.py`; nothing with `EDDIE_DB=off`). A device's baseline starts
learning at its first feed event and becomes active once it has made at
least 200 lookups **and** an hour has passed since the first one
(`EDDIE_BASELINE_LOOKUPS`, `EDDIE_BASELINE_MINUTES`). Lookups are counted as
the feed sees them (no plumbing, 2 s repeats merged), every action included.
Devices are keyed like `GET /devices`: MAC, or client IP (the feed's
`ip:` prefix is dropped).

After learning, the first lookup of a registrable domain the device has never
looked up pushes, after that batch's query events:

```json
{"type": "new_domain", "ts": 1762531200123, "device_mac": "f0:4f:7c:11:22:33", "device": "Fire Stick",
 "domain": "anthropic.com", "query": "api.anthropic.com", "action": "allow"}
```

- `domain` is the registrable domain (eTLD+1); `query` the name looked up;
  `action` the DNS decision (`block` means it tried and was stopped).
- Once per device and domain: the domain joins the baseline. It means "first
  time since learning", not "suspicious": a learning window always misses
  domains a device uses only now and then.
- A domain not looked up for `EDDIE_RETENTION_DAYS` is forgotten with the
  query log, so it counts as new again.
- More than 20 new domains within 24 h (a phone or laptop browsing) makes the
  baseline `unstable`: domains are still recorded, no more `new_domain`
  events are sent until it's reset.

`baseline_state` is pushed when a baseline becomes `active` or `unstable`, or
is `reset`: `{"type", "ts", "device_mac", "device", "state", "domains"}`.
Neither event changes `totals` or is kept for the `snapshot`.

`GET /devices/{key}/baseline` (`404` if none yet, `503` with `EDDIE_DB=off`):

```json
{"device_mac": "f0:4f:7c:11:22:33", "device": "Fire Stick", "state": "learning", "started_at": 1762531200123,
 "active_at": null, "last_seen": 1762531800456, "lookups": 120, "domains": 14, "learned_domains": 14,
 "new_domain_count": 0, "lookups_left": 80, "ms_left": 3000000, "min_lookups": 200, "learn_ms": 3600000,
 "new_domains": []}
```

`lookups` stops counting once learning ends. `new_domains` lists up to 20
domains first seen after learning, newest first, with `first_seen`.
`GET /baselines` lists every device's baseline in that shape.
`DELETE /devices/{key}/baseline` forgets the device's baseline (`404` if
none) and answers `{"device_mac", "reset": true, "domains_deleted"}`; its
next lookup starts learning again.

## Home mode: `GET/POST /mode` (#20)

While home mode is on, the decision engine blocks camera upload domains
(`decision.CAMERA_DOMAINS`, #43: `ring.com`, `arlo.com`, `fakecamera.example`,
`camera.local`, `a.rtmp.youtube.com`, `b.rtmp.youtube.com` and
`upload.youtube.com`, each with its subdomains) on every device with
`source: "home_mode"` and reason `home mode: <domain> camera upload
blocked`. Ring's exact upload hosts aren't confirmed, so all of `ring.com`.
Rules and the allowlist still come first; nothing else changes.

It turns on and off by itself from household phone presence. Mark a phone
with `PUT /devices/{mac} {"phone": true}` (below), using the address it has
on Eddie's network: phones use a private MAC per network, and that is the
one Eddie sees. Every 15 s the API checks the marked phones: one seen in the
last 10 minutes (its `devices.last_seen` from ARP, or a lookup in `queries`)
is home. The first phone home turns home mode on; when the last one has
been gone 10 minutes it turns off. Unmarked devices never count.

`POST /mode {"home_mode": true|false}` is a manual change. It wins over
presence and holds, whoever comes or goes, until `POST /mode {"home_auto":
true}` hands control back (which sets home mode from the phones home right
then). `home_auto` can't go with `home_mode`, and `false` is 422. With
`EDDIE_DB=off` there's no presence check, `home_mode` is a flag in memory,
`home_applied` is always `false` and `home_auto` is 503.

Each change is stored in `engine_state` (`home_mode`, `home_manual`, `home_rev`)
and acknowledged like rules: the DNS server's StoreWatcher records
`home_loaded`, and the answer (and the `mode` event, sent after the wait)
carries `home_applied: true` only once it has. `POST /mode` with home fields
answers `{home_mode, home_manual, home_applied}`.

`GET /mode` returns `home_mode`, `home_manual` and `home_applied`, plus
`offline` and `offline_applied` (below), away mode's fields, and
`quarantine` and `quarantine_applied` (New devices and quarantine, below).

## Away mode: `POST /mode {"away": ...}` (#109)

`POST /mode {"away": true, "away_until": 1762750800000}` (`away_until` is
optional: ms since epoch, in the next 30 days, only with `away: true`;
anything else is 422). `home_mode` may be sent in the same body; away is
handled first, so a failed away request (503 with `EDDIE_DB=off`) leaves home
mode alone.

Turning away on ranks devices from the last week of `queries`
(`eddie/away.py`): the occupancy leak score is the number of hours in which a
device looked up media (`labels.HUMAN`, streaming) or made at least 3× its
median hourly lookups (and at least 30). This is an interim score until the
burglar's-eye report (#34) lands. Devices scoring 0 aren't covered. Security
devices are never covered: Ring, Arlo or Wyze vendors, a label or DHCP
hostname naming a camera, cam, doorbell, alarm or security, or any lookup of a
camera, doorbell or alarm domain this week (only `ring.com` is labelled today).
The covered and exempt devices are stored in `away_devices`, the state in
`engine_state` (`away`, `away_until`, `away_rev`), and the API waits up to 2 s
for the DNS server to write `away_loaded`, the same handshake as rules. It
answers and broadcasts:

```json
{"away": true, "away_until": 1762750800000, "away_applied": true,
 "away_devices": [{"key": "f0:4f:7c:11:22:33", "name": "Fire Stick", "score": 41}],
 "away_exempt": [{"key": "a4:...", "name": "Ring device"}]}
```

`away_applied: false` means stored but no running DNS server confirmed it.
Turning away off clears `away_devices` and answers `away_until: null` and
empty lists. `GET /mode` reports the same five fields; `away` is false once
`away_until` has passed (the DNS engine stops at that time without a reload),
and with `EDDIE_DB=off` it's `away: false, away_applied: false`.

While on, for a covered device only (by its key or client IP), the decision
engine blocks with `source: "away_mode"` a domain labelled telemetry, ads,
analytics or tracking in `eddie/labels.py`, a cached AI `tracker` verdict
(otherwise a flag) and a beacon (otherwise a flag). It comes after rules, the
allowlist, blocklists and the keyword heuristic, so a user allow rule or an
essential still wins. Everything else, including unknown domains, is
unchanged.

## Sensitivity modes: `/sensitivity` (#110)

`POST /sensitivity {"mode": "health", "on": true}` turns one mode on or off.
`mode` is `health`, `kids`, `money` or `beliefs`; anything else is 422. The
state is stored in `engine_state` (`sensitive_<mode>`, `sensitivity_rev`) and
the API waits up to 2 s for the DNS server to write `sensitivity_loaded`, the
same handshake as rules. It answers, and broadcasts as a `sensitivity` event:

```json
{"mode": "health", "sites": 6, "trackers": 74, "examples": ["BetterHelp", "CVS", "GoodRx"],
 "on": true, "protected": ["health"], "applied": true}
```

`sites` and `trackers` are the sizes of the mode's rule set, `examples` up to
three site owners, `protected` every mode now on. `applied: false` means
stored but no running DNS server confirmed it. `GET /sensitivity` answers
`{protected, applied, modes: [{mode, on, sites, trackers, examples}]}`.

Rule set (`eddie/sensitivity.py`): a mode's sites are the domains in
`eddie/labels.py` labelled with its categories (health: `health`; kids:
`kids`; money: `finance`, `shopping`; beliefs: `news`, `religion`,
`politics`). Its trackers are every domain labelled `telemetry`, `ads`,
`analytics`, `tracking` or `id-sync` plus every domain in
`eddie/data/brokers.json`, never one of the sites. While a mode is on, a
device's lookup of one of its sites (or a subdomain) opens a 5-minute window
for that device, whatever the decision for the site. Inside it, after rules,
the allowlist, blocklists, the keyword heuristic and away mode's telemetry
step, the device's lookups of a tracker or of a domain with a cached AI
`tracker` verdict are blocked with `source: "sensitivity"` and a reason like
`health protection: graph.facebook.com is a third-party tracker, looked up
within 5 min of a protected site` (the site itself is never named). Other devices and other lookups are unchanged.
Windows live in the DNS server's memory (at most 10,000 devices) and close
when their mode is turned off.

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
| `action`     | `block` \| `allow` \| `spoof`. `spoof` (#99) answers the domain with Eddie's IP (`EDDIE_SPOOF_IP`) so the stub server (`eddie/spoof_server.py`) returns 200 OK with an empty body over plain HTTP; without `EDDIE_SPOOF_IP` DNS blocks it instead. `spoof` is a normal rule, not a lockdown: `*` takes only `block`. |
| `device_mac` | optional; omit for every device. Use the device's `mac` from `GET /devices` (its MAC, or its client IP while the MAC is unknown). A rule made with a client IP also matches the device after its MAC becomes known. |
| `created_by` | optional, default `"user"`; exactly `user` \| `voice` \| `agent` \| `dashboard` |
| `pinned`     | optional, default `false`. A pinned rule is never rolled back by self-correcting blocks (#101). `POST /rules/{id}/pin` pins a stored rule (404 if there is none) and answers the row with `device`; it changes nothing DNS enforces, so it has no `applied`. |

Answers `201` with the stored row plus the DNS acknowledgment:

```json
{"id": 7, "scope": "device", "device_mac": "172.20.10.5", "pattern": "device-metrics-us.amazon.com",
 "action": "block", "created_by": "voice", "ts": 1762531200123, "pinned": false, "applied": true}
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

## Ask before blocking: `GET /questions`, `POST /questions/{id}` (#79)

AI verdicts only flag (Classifier verdicts below), so a `tracker` verdict
becomes a question for the person instead. `unsure` and `needed` verdicts and
network plumbing never do. The question lives in the API's memory
(`eddie/questions.py`) and is lost on restart.

- **Once per domain:** a domain is offered once per API run; repeats are ignored.
- **Rate limit:** one open question at a time, and a new one at most every
  180 s after the previous one was asked. Trackers that arrive meanwhile wait
  and are grouped by device, up to five domains a question, busiest device first.
- **Device:** the `device` name of the newest feed event for that domain, or
  `null` ("on your network"). It names the question only; Block and Allow
  write global rules.

`GET /questions` lists open questions (at most one), releasing one first if it
is due. Due questions are also released on `/ingest`, `/ai/verdict` and after
an answer. Each is pushed as `question` on `/ws/events`, and the snapshot
carries open ones in `questions`:

```json
{"type": "question", "id": 3, "device": "Laptop", "ts": 1762531200123,
 "domains": [{"domain": "collector-pxabc.px-cloud.net", "reason": "PerimeterX bot fingerprinting"}],
 "prompt": "A new likely tracker on Laptop: collector-pxabc.px-cloud.net. Block it?"}
```

`POST /questions/{id}` with `{"answer": "block" | "allow" | "ignore", "by": "dashboard" | "voice"}`:

- `block` / `allow` writes one global rule per domain with `created_by: by`,
  exactly like `POST /rules` (each sends `rule_change`, and a block is watched
  by self-correcting blocks below), and waits for DNS.
- `ignore` writes nothing; the domain stays flagged and isn't asked about again.
- Answers `{"id", "answer", "rules": [stored rows], "applied"}`. `applied` has
  the `POST /rules` meaning for the last rule (`null` for `ignore`): **only
  `applied: true` may be shown or spoken as blocked or allowed.** Undo is
  `DELETE /rules/{id}` with the returned ids.
- Then pushes `{"type": "question_resolved", "id", "answer", "by", "applied"}`.
- A question is answered once: a second answer, or an unknown id, is `404`.
  Bad input is `422`. With `EDDIE_DB=off`, `block` / `allow` answer `503` and
  the question stays open.

The dashboard shows the open question as a card with Block / Allow / Ignore
(through the bridge below). The voice worker reads it out once when Eddie is
woken ("I've got one question for you. … Say yes to confirm or no to cancel");
a yes answers `block` with `by: voice`, and voice "undo" removes those rules.
No, silence or expiry leave it open on the dashboard. Eddie never asks
unprompted; the opt-in for that isn't built.

## Self-correcting blocks: `rule_rolled_back` (#101)

For 10 minutes after `POST /rules` stores a block rule, the API watches the
query events on `/ingest` (`eddie/rollback.py`; never on the DNS hot path). A
lookup counts as blocked by the rule when it is `action: block`, `source:
rule`, its `reason` names the rule (`device rule <pattern>` or `global rule
<pattern>`) and, for a device rule, the device matches. For each device:

| Sign | Threshold |
|------|-----------|
| `retry_storm` | 12 or more lookups blocked by the rule within 60 s. A healthy device caches the 60 s block answer and asks about once a minute; the feed sends a repeat at most every 2 s, so this is a device asking at least every 5 s. |
| `traffic_collapse` | The device made 20 or more other (not blocked) lookups in the 10 minutes before the rule, has made 3 or more lookups the rule blocked, and at least 3 minutes after the first of them its other lookups since then are at most 10% of its earlier rate. A device that was switched off stops asking for the blocked domain too, so silence alone never counts. |

The first sign on one device rolls the rule back (for a global rule, any
device): the API deletes it through the `DELETE /rules/{id}` handler, so a
`rule_change` `removed` goes out as usual, waits for DNS like any delete, then pushes:

```json
{"type": "rule_rolled_back", "ts": 1762531320000,
 "rule": {"id": 7, "scope": "device", "device_mac": "f0:4f:7c:11:22:33", "device": "Fire Stick",
          "pattern": "device-metrics-us.amazon.com", "action": "block", "created_by": "voice",
          "ts": 1762531200123, "pinned": false},
 "device_mac": "f0:4f:7c:11:22:33", "device": "Fire Stick", "sign": "retry_storm",
 "reason": "I removed the block on device-metrics-us.amazon.com for Fire Stick: it asked for device-metrics-us.amazon.com 14 times in a minute after the block, which can mean it stopped working. That's a guess, not proof it broke. Block it again as a pinned rule to keep it.",
 "applied": true}
```

- `device_mac` / `device` are the device that looked broken; `rule` is the
  deleted row as in `GET /rules`.
- `reason` is the copy to show or say. It always says it's a guess: an app
  may retry a blocked tracker forever and work fine, and a household may just
  stop using a device. Nothing here proves a device broke or that it works again.
- `applied` is the delete's DNS acknowledgment, as for `DELETE /rules/{id}`.
  `false` means the rule is deleted from `eddie.db` but no DNS server confirmed
  it; the dashboard then adds that DNS hasn't confirmed the block is gone.
- Never rolled back: allow and spoof rules, lockdown (`*`), pinned rules, a
  rule pinned or deleted before the rollback runs (or whose id now holds
  another rule), and rules stored before the API started (watches are memory
  only, so an API restart forgets them). The essentials allowlist is
  untouched: a rollback only ever deletes the one rule, and lookups the
  allowlist answers count as the device's other traffic.
- The dashboard shows it as a `[rolled back?]` feed row and a toast with
  `reason`. Voice doesn't announce feed events yet, so it isn't spoken unprompted.

Known gaps: Eddie only sees DNS, so a device that breaks without retrying
(or caches its failure) isn't caught; the feed's 2 s de-duplication caps the
retry count it can see at 30 a minute; and a block that's rolled back can be
re-added and rolled back again unless it is pinned.

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
   "kid": false, "queries": 412, "blocked": 160, "flagged": 12, "trackers": 14,
   "recent_domains": ["device-metrics-us.amazon.com", "api-global.netflix.com", "unagi-na.amazon.com"],
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
| `recent_domains` | up to three domains the device looked up in the window, whatever the answer, newest first: the dashboard shows them so people can tell which device is which |
| `kid` | `true` when the device is marked as a child's (#119): kid mode locks it down. Kept like a label when the device goes quiet |
| `phone` | `true` when the device is marked as a household phone (#20): home mode turns on while one is home. Kept like a label |

Sorted most blocked first. A device's first lookups can be logged under its
IP before the first ARP read (about a second), so a short-lived IP row may
appear next to the MAC row.

`PUT /devices/{mac}` with `{"label": "Fire Stick"}` names a device Eddie has
seen (`404` otherwise); `""` or `null` clears it. Labels are at most 40
printable characters. `{"kid": true}` marks it as a child's device and
`false` unmarks it (#119). `{"phone": true}` marks it as a household phone
for home mode's presence check, `false` unmarks it (#20). A body may carry
any of the three and leaves the rest untouched (`{}` or a non-boolean `kid`
or `phone` is 422). Marking a device writes no rules: kid mode and home
mode do that. Answers with the stored device, its `kid`, `phone` and `name`.

Each device also has `status` (`approved`, `pending` or `denied`; `null` for an
IP-keyed device), and the response has top-level `quarantine` and
`quarantine_applied` (below).

## New devices and quarantine (#98)

`eddie/quarantine.py` keeps a `device_status` row per MAC. The first devices
Eddie records (an empty table) are approved silently: they're the household
it was installed into. After that, a MAC seen for the first time is stored as
`pending`, and the DNS server posts an alert to `POST /devices/new` (a list,
from the device watcher's thread, 1 s timeout, dropped if the API doesn't
answer; the device stays pending either way). The API pushes each one as is:

```json
{"type": "new_device", "ts": 1762560840000, "device_mac": "f0:4f:7c:aa:bb:cc",
 "device": "kindle", "ip": "172.20.10.8", "vendor": "Amazon", "hostname": "kindle", "quarantine": true}
```

`ts` is the ARP read that found it (within about a second of its first
lookup); `device` is its name as in `/devices`; `vendor` and `hostname` are
null when unknown. `quarantine` is whether quarantine was on when it was
recorded: the setting, not proof that DNS is blocking it yet.

`POST /quarantine {"quarantine": true}` turns quarantine on (off by default),
with the same handshake as offline mode: `{"quarantine": true, "applied": true}`
once DNS has written `quarantine_loaded`, `applied: false` if not within 2 s.
Pushed as `{"type": "quarantine", "quarantine", "applied"}`. While on, a
`pending` device gets block answers (`source: "quarantine"`) for everything
but the essentials allowlist, before any rule.

`POST /devices/{mac}/approve` and `POST /devices/{mac}/deny` store a decision
for a recorded MAC (404 unknown, 422 for an IP key) and answer
`{"mac", "name", "status": "approved" | "denied", "applied"}`, also pushed as
`{"type": "device_status", ...}`. A denied device is blocked the same way
whether quarantine is on or not; approving it lifts that.

Known gaps: quarantine is DNS-only, so a device using hard-coded IPs or its
own DNS (DoH) still gets out; router mode (#22/#23) stops that. A new device's
lookups before the first ARP read and the DNS reload (about a second) are
answered normally, and a device ARP never resolves is never quarantined.
A phone with a private Wi-Fi address that rotates shows up as a new device.

## What they know: `GET /what-they-know?hours=24`

`hours` is 1 to 168. Returns `{demo: false, source, hours, facts, text}`.
`source` is `"eddie.db"` when the DNS server's query log (`EDDIE_DB`, read-only)
has lookups in the window, otherwise `"feed"`: the API's in-memory event list
(at most 20,000 events and none older than `EDDIE_RETENTION_DAYS`, lost on restart), which is what `fake_traffic.py` fills.
The two are never mixed. Database rows are named by device label, else
"<vendor> device", else "unlabeled device" (all such devices share that name);
never by MAC, IP or DHCP hostname. Each fact is `{fact, evidence, confidence,
category, sensitive}` computed in `eddie/inferences.py` (tracking company
exposure, active streaming hours, after-midnight streaming sessions, longest
streaming gap, idle telemetry, category by weekday, the device and domain
looked up most on a regular timer, one fact per labelled topic, the tracking
company that sees the most devices, and how many devices are in use).

`category` is one of the README's ten: `Safety`, `Health`, `Kids`, `Money`,
`Household`, `Work`, `Beliefs`, `What's on screen`, `Listening`, `Identity
linking` (`inferences.CATEGORIES`). `sensitive` is true exactly for Health,
Beliefs and Kids. Topic facts (`inferences.TOPICS`) map a `labels.py` category
to a report category, for example `health` to Health or `id-sync` to Identity
linking; their evidence is "N lookups to <companies> from <devices> on N
days", confidence 0.7 across two or more days, else 0.5. After-midnight
streaming counts as Health (a sleep schedule), a beacon to a known ACR domain
as What's on screen, the busiest tracking company as Identity linking, and the
quiet-time facts as Safety. Burglar's-eye facts carry `Safety` too. `text` is a
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
(every device name ends in "(demo)": a living room TV, a kids' tablet, a phone,
a laptop, a kitchen speaker, a thermostat and a doorbell; every event
`demo: true`). It has facts in every category but What's on screen. `text` starts with `label`. Demo events are generated per
request and never enter `/events`, the WebSocket feed or `eddie.db`. The
voice tools only read the live report. An explicit spoken range is sent as
`hours`; with no range, voice and the dashboard omit it so the API uses all
locally retained history. The response includes `requested_hours`, actual
`hours`, `limited_by_retention`, `data_points`, `covered_hours`, and
`enough_data`. Longer requests are capped to retention with a `notice`; fewer
than two observations make Eddie say there is not enough data instead of infer.

## Burglar's-eye report: `GET /burglars-eye?hours=168`

`hours` is 1 to 8760; omitted, a week. Like `/what-they-know`, a request longer
than `EDDIE_RETENTION_DAYS` is cut to it, with `requested_hours`,
`limited_by_retention: true` and a `notice` that `text` starts with. Same
event source and device naming as `/what-they-know`. Returns `{demo: false,
source, hours, requested_hours, limited_by_retention, notice, covered_hours,
full_range, enough, empty_times, devices, text}`, computed in
`eddie/occupancy.py`:

- `covered_hours`: hours from the window's first lookup to now, rounded up.
  `full_range` is true when that is within a seventh of `hours` (a week with
  6 days of data still counts as the week). `text` then starts "From this
  week's data" or "From the last N days"; otherwise "From recent data (2 days,
  not the full week)".
- `enough`: false, with `empty_times` and `devices` empty and `text` saying
  why, when there is no traffic, under 6 hours with any lookup, or no
  streaming at all (home and away can't be told apart).
- Each local hour with a lookup is busy (some device streamed) or quiet. Hours
  with no lookups at all count as neither, since Eddie may have been off.
- `empty_times`: `{when, pattern, fact, evidence, confidence}`. With at least
  2 days on which Eddie saw half the waking hours (6 AM to midnight), a
  pattern: waking hours quiet on at least 80% of the days seen, per weekdays
  and weekends when each has 2 such days, else "most days", in stretches of 2
  hours or more. With fewer, `pattern: false` and the quiet stretches of each
  day, and `text` says it isn't a pattern yet.
- `devices`, highest `leak_score` first: `{device, leak_score, lookups,
  blocked, security, never_block, per_busy_hour, per_quiet_hour, said}`.
  `leak_score` is `round(100 * |a - b| / (a + b))` of lookups per busy hour
  (a) and per quiet hour (b); `null` (and no per-hour fields) under 10
  lookups. `security` is true when the device looked up a camera, doorbell or
  alarm domain (`labels.SECURITY`); `never_block` mirrors it as advice only,
  nothing enforces it yet (#46).

`?demo=true` ignores `hours` and runs the same code on the demo week, with
`demo: true`, `label`, and `text` starting with `label`.

## Privacy settings coach: `GET /suggestions` (#112)

Guidance only; nothing is written. Reads the devices table and the last week
of the query log (`eddie/coach.py`) and returns `{devices, guides, text}`:

- `devices`: `{key, name, kind, guide}` for each recognised device, by kind
  then name. `kind` is `fire_tv` or `ring`. `guide` is `{name, where, spoken,
  steps: [{setting, do, why}], note, sources: [{title, url}]}`.
- `guides`: the device types Eddie has a guide for (`["Fire TV", "Ring"]`).
- `text`: one sentence per kind ("For Fire Stick and Bedroom TV, on the TV
  open Settings, …"), ending "I haven't changed any of these myself. The steps
  and sources are under suggestions on the dashboard." With nothing
  recognised, it names the guides Eddie has.

503 `Device log unavailable.` when `EDDIE_DB` is `off`, missing or unreadable.
The dashboard bridge relays it read-only as `GET /api/suggestions`.

## Local voice bridge (port 5173)

The dashboard bridge SSE endpoint `/events` emits `voice_status` with `status` (`off`, `starting`, `ready`, `error`), optional `engine` (`vosk` prototype or `openwakeword`) and `message`. `GET /api/voice/status` returns the latest status. Start/stop POST endpoints operate the local microphone. These are separate from the traffic API on port 8000.

Relays to the traffic API (`EDDIE_API_URL`, default `http://127.0.0.1:8000`), so the page needs no CORS or direct API access. Each answers 502 `{message}` if the API errors or doesn't answer, never an empty or guessed result:

| Bridge | Traffic API | Notes |
|--------|-------------|-------|
| `GET /api/rules` | `GET /rules` | read-only |
| `GET /api/devices` | `GET /devices?hours=24` | read-only |
| `GET /api/mode` | `GET /mode` | read-only |
| `GET /api/questions` | `GET /questions` | read-only |
| `POST /api/questions/answer` `{id: int, answer}` | `POST /questions/{id}` with `by: "dashboard"` | answers with the API's reply (`applied: false` passed through); `404` if already answered |
| `POST /api/questions/undo` `{id: int}` | `GET /rules`, then `DELETE /rules/{id}` | deletes only the rules this bridge's answer to that question wrote, if each is still that `dashboard` rule; `{removed, applied}` |
| `GET /api/what-they-know[?demo=1]` | `GET /what-they-know` (all retained data) or `?demo=true` | read-only |
| `POST /api/mode` `{home_mode: bool}` | `POST /mode` | answers with the API's echo `{home_mode}`; away mode isn't relayed, so a paired device can't set it |
| `POST /api/offline-mode` `{offline: bool}` | `POST /offline-mode` | answers with `{offline, applied}`; `applied: false` is passed through |
| `POST /api/quarantine` `{quarantine: bool}` | `POST /quarantine` | answers with `{quarantine, applied}` |
| `POST /api/devices/decide` `{mac, decision: "approve" \| "deny"}` | `POST /devices/{mac}/{decision}` | answers with the API's `{mac, name, status, applied}`; anything but a MAC and approve/deny is 400 |
| `POST /api/devices/update` `{mac, label?, kid?}` | `PUT /devices/{mac}` | names a device or marks it as a child's (#119); `mac` is a MAC or client IP; answers with the API's stored device; 400 for anything else, 422 for a bad name, 404 for a device Eddie hasn't seen |
| `POST /api/kid-mode` `{on: bool}` | `GET /devices`, `GET /rules`, then `POST` / `DELETE /rules` per child's device | runs kid mode with the page's confirm dialog as the yes (`voice_agent.kid_mode_change`, rules `created_by: dashboard`); answers `{message}`, the same wording voice speaks, which says whether DNS loaded the rules |

The mode POSTs forward only that one boolean (anything else is 400) and wait up to 5 s for the API.

Control auth (#40): POSTs need a `Host` that is an IP, `localhost` or `.local`, and a same-origin `Origin` if one is sent (403 otherwise). From the Eddie computer (loopback) no token is needed. From another device, only `POST /api/voice/ask` (`{text}`), `POST /api/mode`, `POST /api/offline-mode`, `POST /api/quarantine`, `POST /api/devices/decide`, `POST /api/devices/update`, `POST /api/kid-mode` and the two `/api/questions` POSTs are allowed, with `Authorization: Bearer <pairing token>`; a missing or wrong token gets 401 `{message}`. `/api/voice/start`, `/api/voice/stop`, `/api/voice/dismiss` and `/api/events` answer 403 from other devices whatever the token. A paired `/api/voice/ask` of “stop recording” still turns the mic off, as the spoken command does (#128).

Wake lifecycle: `wake`, `voice_state: listening`, `voice_state: thinking`, optional `transcript`, `agent_reply`, `speaking`, `speech_end`, then `voice_state: listening` again for a follow-up without the wake phrase. If no speech starts within 4 seconds of listening, the worker emits `voice_state: dismissed` and the dashboard closes Eddie and returns to the live feed; the next request needs the wake phrase. No transcript is persisted. Capture or inference failure yields `voice_status: error`.

A timer or reminder coming due (#122) sends `voice_state: thinking`, `agent_reply`, `speaking` with `title: "Timer done"` or `"Reminder"`, then `speech_end`, with or without the microphone on. If the alarm sound can't play, a `voice_status` with `message: "Alarm sound unavailable: …"` comes first and Eddie still speaks.


Voice recovery: after an initial wake with no command, or an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again. Four seconds of silence during a follow-up dismisses Eddie back to the newest feed activity. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.

Active conversation lifecycle: listening state events include `is_eddie_active: true`; after four seconds with no user speech, `voice_state: dismissed` includes `is_eddie_active: false`. This applies immediately after the wake and after every reply. Two empty transcriptions in a row also end with `dismissed`. `POST /api/voice/dismiss` (the overlay's close button; localhost only, `{"ok": true}`) ends an active conversation: the worker emits `voice_state: dismissed` instead of `listening` and waits for the wake phrase again. It also cancels any pending confirmation.

Confirmation: after Eddie reads back a risky action, the bridge emits `voice_state: confirming` with `kind`, `prompt` and `expires_in_ms` in place of `idle`; the worker's following `listening` event does not clear it. The next `thinking`, `dismissed` or the expiry clears it. Action arguments are never sent. The dashboard shows `prompt` (Eddie's read-back; for `kind: text` it contains the message being sent, word for word) under Eddie with “say yes to confirm or no to cancel” until then (#19).

## Company counts (#25)

`GET /summary` adds `companies` (top ten `{company, requests, blocked, tracking_requests}` rows) and `company_window_seconds: 3600`. Counts include only timestamps in the past hour, exclude future events, and use the same in-memory event window as the spoken summary. A request is a DNS lookup, not evidence that data was uploaded. Unknown owners fall back to a registrable domain using the bundled Public Suffix List, including private suffixes.

## Per-device counts and event filters (#29)

`GET /summary?window=15m|1h|24h` (default `1h`; anything else gets 422) adds `window` and `devices`: one row per device label seen in that window, `{device, requests, blocked, flagged, blocked_companies}`, busiest first, where `blocked_companies` is up to three `{company, blocked}` rows. `text` and `companies` still cover the last hour. Like the rest of `/summary`, counts come from the API's in-memory feed (at most 20,000 events, none older than `EDDIE_RETENTION_DAYS`), so a busy 24-hour window can be partial.

## Cross-device tracker map: `GET /tracker-map` (#107)

`GET /tracker-map?window=1h|24h` (default `24h`; anything else gets 422) says which tracking companies each device looked up: `{window, device_count, devices, companies, text}`. `devices` is every device label with any lookup in the window, so `device_count` is the "of your N devices" denominator. `companies` is up to twelve rows, most devices first, built only from lookups whose `eddie/labels.py` category is a tracking one (`telemetry`, `ads`, `analytics`, `tracking`, `id-sync`): `{company, device_count, requests, blocked, devices: [{device, requests}], id_sync}`. `id_sync` lists that company's ad-ID sync domains (category `id-sync`, e.g. `rlcdn.com`, `tapad.com`) as `{domain, requests, blocked, devices, rule_id}`; `rule_id` is the id of an every-device block rule on `domain` or `*.domain`, or `null` (always `null` when `EDDIE_DB=off`). A rule is stored intent, not proof DNS is enforcing it. `text` is one sentence, e.g. "Google sees 4 of your 6 devices.", or "No tracking lookups in this window." A count is a DNS lookup, not proof of what was sent. Like `/summary`, it reads the in-memory feed, so a busy 24-hour window can be partial.

`POST /tracker-map/block {"domain": "rlcdn.com"}` blocks one ad-ID sync domain and its subdomains on every device: it writes `*.rlcdn.com` through the same path as `POST /rules` (`created_by: "dashboard"`, watched for breakage, a `rule_change` broadcast) and answers with the rule, `existing: false` and DNS's `applied`. If an every-device block rule on that domain already exists it writes nothing and answers with that rule, `existing: true` and `applied: null`. Any domain that isn't exactly an `id-sync` key in `labels.DOMAINS` gets 422; this endpoint can't block anything else.

The dashboard bridge relays the map read-only as `GET /api/tracker-map` (24-hour window) and the block as `POST /api/tracker-map/block`, a paired control.

## Data broker watch

`GET /summary` also adds `brokers` over the same `window` as `devices`: `{requests, blocked, devices, companies}`, where `devices` is one `{device, requests, blocked}` row per device that looked up a known broker domain and `companies` is up to ten `{company, kind, requests, blocked}` rows, busiest first; `kind` is `data broker` or `ad exchange`. A domain counts when it or a parent domain is in `eddie/data/brokers.json` (source in `eddie/data/README.md`). A count is a DNS lookup, not proof that data was sold, and a broker missing from the list is not counted. The dashboard shows a `brokers:` counter next to allowed and blocked, and highlights those feed lines with their kind.

`GET /events` also takes `action=allow|block|flag` (anything else gets 422) and `domain=` (exact, already-normalized match). The voice agent uses these for recent blocks and domain explanations.


## Classifier verdicts (#27)

`POST /ai/verdict` takes `{domain, label, confidence, reason, ts}`. Label is `tracker|needed|unsure`, confidence is a finite number in [0,1], reason is printable, at most 14 words and 120 characters, and ts is epoch milliseconds. Unknown fields and invalid values get 422. A successful response is `{ok: true}` and broadcasts `{type: "ai_verdict", ...payload}` to `/ws/events`. This event does not change historical queries, counters, or rules; later DNS decisions use the cached verdict. Like every write, it needs the API token from anywhere but loopback (see Access below). The classifier refuses external API URLs and redirects. The outbox is bounded to 1,000 in-memory events, retried without repeating inference; it is not a durable notification log.


## Real AI on the scripted demo feed

`python -m eddie.feed_classifier` polls the local API for scripted demo queries and feeds the real bounded Ollama worker using an in-memory SQLite queue/cache. Real DNS still uses the durable database worker. `run_all.sh` launches the adapter only with the fake-traffic profile. Stopping it loses its cache, and no demo network history is added to disk. It polls up to 2,000 recent events once per second and retains at most 20,000 query rows for an hour; heavy traffic can outrun that window. Inference remains on the host with local-only URLs, not in the DNS or API request path.

The API retains at most 1,000 verdicts in memory, returns them from `GET /ai/verdicts`, and includes `ai_verdicts` in WebSocket snapshots so a browser refresh does not hide completed results. The fake generator uses these real cached verdicts for later decisions, while still labelling the traffic itself as scripted demo traffic in its launcher/docs. The old hard-coded “sent to AI” and fabricated AI confidence text are removed. Unknowns say “awaiting classification”; an AI line (`[approved (ai)]`, `[unsure (ai)]` or `[flagged (ai)]`, coloured like that DNS result and stamped with the verdict time) is printed only after actual model output is validated; snapshot verdicts are shown among the DNS lines in time order. A tracker reads flagged, never blocked, because the AI does not block. These verdicts do not prove anything about real Fire Stick traffic.
