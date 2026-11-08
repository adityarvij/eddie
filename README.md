# Eddie — Local Privacy Guard

**Pitch:** Pi-hole is a tool you configure. Eddie is a guard you hire. Eddie sits between your smart devices and the internet, shows you live who is trying to track you, blocks it, and talks to you about it. Everything runs on the box, so the guard never becomes a spy.

**Tracks:** Privacy-First (main) + Most Startup-able. Optional Surge if Eddie uses Tavily/Exa to research unknown domains (domain name only, never user data).

## How it works

1. Every device is set to use Eddie (the Pi) as its DNS server. For the demo, the Fire Stick's DNS is set by hand; a production Eddie sits in the network path so nothing can skip him.
2. Every device must ask Eddie for directions (DNS) before it can send anything. Eddie logs every request.
3. Eddie decides: your rules → known-good list → tracker blocklist → local AI for anything unknown → flag if unsure.
4. Blocked requests get a dead-end answer. The data never leaves the house.
5. The screen shows every decision live. "Hey Eddie" lets you ask and command by voice.

## Run it (development)

```bash
./scripts/run_all.sh            # API + fake traffic in Docker, dashboard on the host
./scripts/run_all.sh --no-fake  # API only, for a real DNS server posting to :8000
```

Dashboard at http://localhost:5173, API at http://localhost:8000. The traffic API and `fake_traffic.py` run in Docker (`compose.yaml`) with `restart: unless-stopped`, so they keep running until `docker compose down`. The dashboard (`frontend/serve.py`, launched through `scripts/run_voice.sh` with the Vosk wake prototype and Piper voice) stays on the host because it also runs the voice loop, and Docker on macOS can't reach the microphone or speaker; Ctrl-C stops it. Ollama also stays on the host for Metal GPU access. For the iPad, run `EDDIE_BIND=0.0.0.0 EDDIE_DASHBOARD_HOST=0.0.0.0 ./scripts/run_all.sh` and open `http://<Mac IP>:5173`: the API and the dashboard then accept network connections, the page finds the API on the same host, and the mic controls stay on the Mac. The fake traffic's verdicts, including the "AI:" ones, are scripted, not real classifier output. `run_all.sh` gives the API, fake traffic, classifier and voice bridge a shared random `EDDIE_API_TOKEN`, because the containerised API sees host callers as remote; export your own before running it to give the same token to a DNS server started in another shell.

## Stack (hackathon version)

- **Network core:** Python + dnslib (DNS server) on the Raspberry Pi; devices point their DNS at it; optional traffic monitor for non-DNS traffic (`eddie/traffic.py`, header counts only, router mode)
- **Rules/data:** SQLite; public blocklists (StevenBlack, Pi-hole lists)
- **AI brain:** Ollama + Llama 3.2 3B or Qwen, using tool calling (read logs, block/allow, send email)
- **Voice:** openWakeWord ("Hey Eddie"), faster-whisper (speech to text), Piper (voice) — all offline
- **Backend:** FastAPI + WebSocket for live events
- **Dashboard:** React or plain HTML, full-screen kiosk mode
- **Email action:** Gmail API — the only deliberate internet call, only on request

**Post-hackathon:** rewrite the network core (DNS server, traffic monitor) in Rust on Tokio; AI and voice stay in Python behind a local API.

## Must-have features (MVP)

1. **DNS redirect** — devices use Eddie as their DNS server, so every lookup goes through him
2. **DNS logging** — every domain each device requests, with timestamps
3. **Device identification** — label devices ("Fire Stick") by MAC/hostname
4. **Decision engine** — user rules, allowlist, blocklist, AI for unknowns, flag if unsure
5. **Blocking** — dead-end answers for trackers; streaming still works
6. **Local AI classifier** — labels unknown domains tracker / needed / unsure, with a one-line reason
7. **Live idle screen** — scrolling feed: "Fire Stick just tried to reach device-metrics — blocked", tracker counter
8. **"Hey Eddie" voice** — wake word → "Hey, what's up?"
9. **Voice update** — "What's been going on?" → spoken summary of the last hour/day
10. **Voice commands** — "Block it", "Allow Netflix", "Lock down the TV"
11. **Offline proof** — pull the internet mid-demo; Eddie keeps working
12. **Send an email** — one personal-assistant action, the only time Eddie touches the internet
13. **Home mode (priority)** — when a household phone is on the Wi-Fi, Eddie blocks indoor cameras' cloud uploads (Fire Stick in the demo); the moment everyone leaves, he unblocks them so the cameras show up in the app again; "Hey Eddie, turn off home mode" overrides

## Extension features

### Smarter guarding
- **Self-testing blocks** — block, check the device still works, auto-rollback if it broke
- **Device baselines** — learn what each device normally talks to; alert on anything new
- **Idle snitching detector** — flags devices phoning home when nobody is using them
- **Fake data mode** — feed dummy responses to endpoints that break when blocked (implemented, #99: rule `action: spoof` plus `eddie/spoof_server.py`; plain HTTP only)
- **Geo alerts** — "Your doorbell started talking to a server in another country"
- **Plain-English goals** — "The TV can talk to Netflix and nothing else" becomes rules automatically

### Reporting
- **Weekly privacy report** — "Your TV tried to contact Amazon's tracking servers 2,000 times this week" (Eddie sees where data goes, not what's inside it)
- **Privacy score per device** — grade each device A–F on how much it tracks
- **Company breakdown** — who is watching you most (Amazon, Google, ad networks)
- **Shareable receipt** — exportable proof of what was blocked

### Household
- **Kid mode** — lock down a child's tablet by voice
- **Guest mode** — Airbnb/short-term rental: guests see that the house isn't watching them
- **Multiple voices** — recognize household members, with per-person permissions

### Personal assistant
- Reminders and timers
- Calendar summary
- Draft and send texts/emails
- Control local smart-home devices (lights, plugs) without cloud apps
- "Read me my day" morning brief

### Hardware
- Raspberry Pi 5 box with touchscreen
- LED ring that pulses red when something gets blocked
- Physical "privacy kill switch" button that cuts all trackers instantly

## Demo script

1. Fire Stick boots with its DNS pointed at Eddie — trackers pop up on the iPad live
2. "Hey Eddie, what's the TV doing?" → spoken answer
3. "Block it" → feed turns red, Netflix still plays
4. Home mode: Adi's phone is on the Wi-Fi → the Fire Stick goes quiet; phone leaves the Wi-Fi → it reconnects. Pitch: the same thing keeps strangers from watching you through Ring indoor cameras
5. Simulated living-room camera on the dashboard: uploading with a live frame → Adi's phone joins the Wi-Fi → "Upload blocked: home mode" → phone leaves → uploads resume. Preferred if router mode works: a real phone camera streaming to YouTube Live, cut mid-stream by home mode (see "Home mode demo with a real stream")
6. Pull the internet → Eddie still works (proves local)
7. "Hey Eddie, email Will the report" → assistant action
8. Pitch: buyer (privacy-conscious homes, Airbnb hosts) and why now

Rehearse Sunday morning and record a backup video.

## Team split

- **Will:** DNS server, decision engine, logging, blocking, Pi setup
- **Adi:** AI classifier, dashboard, voice, email action
- **Checkpoint:** Saturday ~6 PM, wire the two halves together

## 24-hour plan (Sat 11 AM to Sun 11 AM)

| Time | Will | Adi |
|---|---|---|
| 11:00–11:30 | **Together:** lock the event format, API routes, tables and agent tools; create the repo | |
| 11:30–2:00 | DNS server on the Mac: forwarding, blocking, SQLite logging, blocklists and allowlist; test with a phone pointed at the Mac | FastAPI + WebSocket + fake traffic feed; live feed screen tested on the iPad; start the voice overlay |
| 2:00 | **Together:** first end-to-end check (one fake lookup → DNS → screen → database) | |
| 2:00–5:00 | Decision engine order, beacon and idle detection, home and away mode with phone presence | Ollama tool-calling agent (typed first, then voice loop); "What they know about you" card with hard-coded facts |
| 4:00 | Whoever can step away: Private and Local AI workshop, Curry 348 | |
| 5:00–7:00 | **Together:** hardware arrives. Fire Stick on the monitor, DNS pointed at Eddie, record real traffic; speakerphone into the Mac; Pi running Eddie (stay on the Mac if it fights you) | |
| 7:00–1:00 AM | AI classifier with cached verdicts, company mapping, plain-English rules | Real facts into the "What they know" card with the number check; device panel, mode toggles, voice commands (block it, I'm home, undo) |
| Midnight | **Together:** whole demo works start to finish, even if rough | |
| 1:00–6:00 AM | Sleep in shifts (3–4 hours each); stretch goals only with spare energy: email action, self-correcting blocks, morning summary | |
| 6:00–9:00 AM | Polish and fix bugs; feature freeze at 9 | |
| 9:00–11:00 AM | Rehearse 3+ times, record backup video, write the pitch (Ring and Alexa story, why local AI, who buys it), submit before 11 | |

**Cut list if behind:** email first, then the Pi, then the device panel. Never cut the live feed, "Hey Eddie", home mode or the "What they know" card.

## Hardware setup and kit

### Roles

| Device | Job |
|---|---|
| iPhone | Personal Hotspot over Wi-Fi: the network everything joins (venue Wi-Fi may block devices from seeing each other). Keep it plugged in. |
| Raspberry Pi 5 (CanaKit) | Eddie's box: DNS server, decision engine, SQLite, dashboard web server |
| MacBook Pro (M1, 16GB) | Eddie's brain: Ollama model and the voice loop |
| USB speakerphone | Plugs into the Mac for "Hey Eddie" |
| Fire TV Stick + borrowed HDMI monitor | The device Eddie catches (demo only, not part of the product) |
| iPad | Eddie's face: dashboard in Safari, propped next to the Pi (not on its fan) |

**Kit arriving ~5 PM Saturday:** Pi kit, Fire Stick, speakerphone. The Wi-Fi adapter isn't needed with the DNS plan; return it if unused.

**Bring or borrow:** HDMI monitor or TV for the Fire Stick, power strip, spare USB-C cable.

**Pi prep (night before):** Raspberry Pi Imager → Raspberry Pi OS (64-bit) → settings: username and password, iPhone hotspot Wi-Fi name and password, SSH on. The Pi then boots straight onto the hotspot; control it from the Mac with `ssh <user>@eddie.local`. No keyboard, mouse or monitor needed.

### 5 PM setup checklist

1. Power the Pi; `ssh <user>@eddie.local` from the Mac.
2. Get the Pi's IP with `hostname -I` (usually `172.20.10.x` on an iPhone hotspot).
3. Clone the repo onto the Pi; start Eddie (`sudo .venv/bin/python -m eddie.dns_server`, see "Running the DNS server").
4. Check from the Mac: `dig @<Pi IP> google.com`.
5. Fire Stick: Settings → Network → forget and reconnect → Advanced → DNS 1 = Pi's IP. Watch lookups scroll.
6. iPad: open `http://<Pi IP>:8000`, Add to Home Screen.
7. Mac: `OLLAMA_HOST=0.0.0.0 ollama serve` so the Pi can reach the model.
8. Record real Fire Stick traffic around 5 to 6 PM as a replay backup.

**Fallback:** if the Pi fights you, run the DNS server and dashboard on the Mac and point the Fire Stick and iPad at the Mac's IP. Nothing else changes.

Keep boxes and receipts so anything unused can be returned.

## Agent build spec: architecture

Hand this section and the ones below to a coding agent (Claude Code etc.). Target: a working demo in 24 hours on one MacBook, Python 3.11+, everything offline except the explicit email action.

### Runtime topology

1. Every device joins the iPhone Personal Hotspot over Wi-Fi.
2. The Raspberry Pi runs `eddie-dns` on port 53 (needs sudo), plus the decision engine, SQLite and FastAPI with the dashboard on port 8000. Fallback: the Mac runs these instead (tested: dnslib binds fine on macOS when Internet Sharing is off).
3. The Mac runs Ollama (`OLLAMA_HOST=0.0.0.0 ollama serve`) and the voice loop with the USB speakerphone. The Pi calls the model at `http://<Mac IP>:11434`.
4. Devices to protect have DNS set manually to the Pi's IP (Fire Stick: Settings → Network → Advanced → DNS 1). No hotspot or DNS takeover needed.
5. The iPad shows the dashboard at `http://<Pi IP>:8000`.
6. Upstream resolver for allowed queries: `1.1.1.1` or `9.9.9.9`.
7. Known limit: apps with hardcoded or encrypted DNS can skip Eddie. A production Eddie fixes this by being the router or sitting between modem and router.

### Processes / modules

| Module | Language / libs | Job |
|---|---|---|
| eddie-dns | Python, dnslib, asyncio | Answers every DNS query from devices pointed at Eddie; calls the decision engine; forwards or sinkholes; emits events |
| decision | Python | Ordered rules: user rules → allowlist → blocklist + keyword heuristics → cache → AI classifier → default allow + flag |
| classifier | Ollama HTTP API on the Mac (port 11434), Llama 3.2 3B or Qwen 2.5 3B | Labels unknown domains tracker / needed / unsure with a reason and confidence; runs async so DNS never waits on it |
| devices | Python, reads the Pi's ARP table (`ip neigh`) | Maps client IP → MAC → friendly name ("Fire Stick") via hostname, MAC vendor prefix (OUI) and user labels |
| store | SQLite (`eddie.db`) | Queries log, decisions, rules, devices, AI verdict cache, traffic counts |
| traffic | Python, Linux raw socket (optional, off by default) | Counts bytes and packets per device, remote IP, port and protocol from packet headers on the LAN interface, so upload volume, hard-coded IPs and long streams show up. Only useful when Eddie is the router (#22) |
| api | FastAPI + uvicorn, WebSocket | REST for dashboard and agent tools; `/ws/events` pushes each decision live |
| dashboard | Plain HTML/JS or React, kiosk full-screen | Black, minimal live feed; counters; per-device view; listening animation |
| voice | openWakeWord, faster-whisper (base.en), Piper TTS, sounddevice | Wake word "Hey Eddie" → record until silence → transcribe → agent → speak reply |
| agent | Ollama tool calling | Turns speech into tool calls (summaries, block/allow, email) and a short spoken answer |
| email | Gmail API (OAuth desktop flow) | The only deliberate outbound call; sends only after spoken confirmation |

### Request flow (one DNS query)

1. Fire Stick asks the Pi for `device-metrics-us.amazon.com`.
2. `eddie-dns` looks up client IP → device.
3. `decision.evaluate(domain, device)` returns `ALLOW`, `BLOCK` or `FLAG` plus reason and source (rule / allowlist / blocklist / heuristic / ai / default).
4. `ALLOW` or `FLAG`: forward to upstream, return the real answer. `BLOCK`: return `0.0.0.0` (A) and `::` (AAAA) with TTL 60. `SPOOF` (a rule with `action: spoof`, #99): return Eddie's IP (`EDDIE_SPOOF_IP`) for A and an empty answer for AAAA, so `eddie/spoof_server.py` answers plain HTTP with 200 OK and an empty body. Without `EDDIE_SPOOF_IP` a spoof rule blocks. HTTPS clients fail the TLS handshake (no certificate for the tracker's name), the same as a block.
5. Write the row to `queries`; publish event to the WebSocket.
6. If the domain was unknown, queue it for the classifier; when the verdict lands, cache it and push an `ai_verdict` event. Subsequent queries use the cached verdict.

**Latency budget:** under 20 ms added per query on the hot path. The AI never blocks a DNS reply.

### Running the DNS server

`eddie/dns_server.py` implements steps 1, 3 and 4 above (UDP only) and the `queries` half of step 5: every lookup writes one row to `eddie.db` through `eddie/store.py`. Each lookup is also sent to the live feed (`POST /ingest`, then `/ws/events`) by `eddie/feed.py`, off the reply path: about 25 ms from lookup to dashboard on the development Mac. Start the API without fake traffic (`./scripts/run_all.sh --no-fake`) so the screen shows only real lookups. Step 2 is `eddie/devices.py` (#14): the client IP maps to a MAC from the ARP table, so `queries.device_mac` and the feed's `device_mac` hold the MAC, and the feed's `device` is the device's label, DHCP hostname or vendor ("Amazon device"). A client ARP can't resolve stays `ip:<IP>` with the IP as its name. Devices that look something up are recorded in the `devices` table; `GET /devices` names and counts them. `eddie/decision.py` implements the full decision order below. At startup the DNS server loads every `blocklists/*.txt` file (StevenBlack hosts, the Perflyst smart-TV and Fire TV lists, and Eddie's own `eddie-extra.txt`: about 78,000 domains in 0.3 s) and the essentials in `allowlist.txt` (`eddie/lists.py`). Rules come from the `/rules` API (`eddie/rules.py`).

Spoof rules (#99) need two more things on the Pi: `EDDIE_SPOOF_IP=<Pi's LAN IPv4>` for the DNS server, and the stub server on port 80 (`sudo .venv/bin/python -m eddie.spoof_server`; development: `EDDIE_SPOOF_PORT=8080 uv run python -m eddie.spoof_server`). Nothing listens on 443, so HTTPS uploads to a spoofed name just fail.

### Running the classifier

`eddie/classifier.py` labels unknown domains with the local model. Its queue is the query log itself: flagged `queries` rows from the last hour with no `ai_verdicts` row yet. Each verdict is written to `ai_verdicts`, and the DNS server reloads rules and verdicts from `eddie.db` within about 0.2 s, so the next lookup of that domain uses it (about 2.5 s end to end with `llama3.2:3b` on the development Mac). DNS never waits on the model. Each verdict is also sent to the live feed as an `ai_verdict` event (`POST /ai/verdict`). Network plumbing (reverse lookups, service-discovery names, `.local`) is never queued, since it isn't a company to judge.

```bash
uv run python -m eddie.classifier   # needs Ollama running and the same EDDIE_DB as the DNS server
```

Settings: `EDDIE_OLLAMA_URL` (default `http://127.0.0.1:11434`; must be `localhost` or a loopback or private IP literal, so on the Pi use `http://<Mac IP>:11434`), `EDDIE_CLASSIFIER_MODEL` (default `llama3.2:3b`). With `EDDIE_DB=off` there's no queue, and the classifier refuses to start.

```bash
uv run python -m eddie.dns_server                      # Mac: 0.0.0.0:53 binds without sudo on recent macOS
sudo .venv/bin/python -m eddie.dns_server              # Pi / Linux: port 53 needs root
EDDIE_DNS_PORT=8053 uv run python -m eddie.dns_server  # development, any OS: dig @127.0.0.1 -p 8053 google.com
```

Settings: `EDDIE_DHCP_LEASES` (dnsmasq leases file for device hostnames; default `/var/lib/misc/dnsmasq.leases` if it exists, `off` for none), `EDDIE_DNS_HOST` (default `0.0.0.0`), `EDDIE_DNS_PORT` (default `53`), `EDDIE_DB` (default `eddie.db` in the working directory, created `0600` on first run; `off` logs nothing), `EDDIE_RETENTION_DAYS` (default `7`; older rows are securely deleted at start and hourly), `EDDIE_BLOCKLISTS` (default the repo's `blocklists/` directory; `off` loads no blocklists), `EDDIE_ALLOWLIST` (default the repo's `allowlist.txt`; if it's missing the server refuses to start, because blocking without it can break devices), `EDDIE_UPSTREAMS` (default `1.1.1.1,9.9.9.9`, tried in order with a 2 s timeout each; if all fail the device gets SERVFAIL), `EDDIE_API_URL` (default `http://127.0.0.1:8000`; where lookups are sent for the live feed; must be `localhost` or a loopback or private IP literal), `EDDIE_FEED` (`off` sends nothing to the live feed; if the API is down, events are dropped and DNS carries on), `EDDIE_API_TOKEN` (sent to the API as a bearer token; needed when the API isn't on this machine's loopback, such as the Docker API, which takes writes from elsewhere only with it), `EDDIE_BLOCK_DOH` (default `off`; `on` blocks the encrypted DNS resolver names in `eddie/bypass.py`, #97). On the Mac, Internet Sharing must be off because it holds port 53. Binding `127.0.0.1:53` still needs sudo on macOS.

## Agent build spec: data, API, tools

### SQLite schema

```sql
CREATE TABLE devices (mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, vendor TEXT, label TEXT, first_seen INTEGER, last_seen INTEGER);
CREATE TABLE queries (id INTEGER PRIMARY KEY, ts INTEGER, device_mac TEXT, domain TEXT, qtype TEXT, action TEXT, source TEXT, reason TEXT);
CREATE TABLE rules (id INTEGER PRIMARY KEY, scope TEXT, device_mac TEXT, pattern TEXT, action TEXT, created_by TEXT, ts INTEGER);
CREATE TABLE ai_verdicts (domain TEXT PRIMARY KEY, label TEXT, confidence REAL, reason TEXT, ts INTEGER);
CREATE TABLE engine_state (key TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE away_devices (mac TEXT PRIMARY KEY, score INTEGER, security INTEGER);
CREATE TABLE traffic (id INTEGER PRIMARY KEY, first_ts INTEGER, last_ts INTEGER, device_mac TEXT, ip TEXT, port INTEGER, proto TEXT, bytes_out INTEGER, bytes_in INTEGER, packets_out INTEGER, packets_in INTEGER);
CREATE INDEX q_ts ON queries(ts);
CREATE INDEX q_dev ON queries(device_mac, ts);
CREATE INDEX t_dev ON traffic(device_mac, last_ts);
```

`rules.pattern` matches exact domain or suffix (`*.amazon-adsystem.com`). `scope` = `global` or `device`. `created_by` = `user` (default) / `voice` / `agent`. `traffic` rows are written by the optional traffic monitor: one per device, remote IP, remote port and protocol per minute it saw packets (`port` is 0 for ICMP), with bytes and packets each way. `engine_state` holds `rules_rev` (bumped by every rule write) and `rules_loaded` (the revision the DNS engine last loaded), which `/rules` uses to acknowledge a change; offline and away mode keep their state and revisions there the same way. `away_devices` holds the devices away mode covers (`security = 0`, with their leak score) or exempts (`security = 1`) while it's on, and is emptied when it's turned off.

### Decision order (first match wins)

1. Device rule, then the device's lockdown rule (`*`, block only; an allowlisted domain still passes), then global rule from `rules`.
2. **Allowlist:** essentials so nothing breaks (Netflix, YouTube video CDNs, Amazon content and auth, Apple, NTP, captive-portal checks). Ship as `allowlist.txt`.
3. **Blocklist:** StevenBlack hosts + a smart-TV list, loaded into a Python set at startup (`blocklists/*.txt`, downloaded once before the event). Then, only with `EDDIE_BLOCK_DOH=on`, the encrypted DNS resolver names in `eddie/bypass.py` (`dns.google`, `cloudflare-dns.com`, …) → `BLOCK`, source `blocklist`, reason "encrypted DNS resolver dns.google" (#97).
4. **Keyword heuristic:** labels containing `metrics`, `telemetry`, `analytics`, `ads`, `adsystem`, `tracking`, `beacon`, `crashlytics`, `log` → `BLOCK`, source `heuristic`.
   **Away mode** (#109): for a device away mode covers, until its end time, a domain labelled telemetry, ads, analytics or tracking in `eddie/labels.py` → `BLOCK`, source `away_mode`; steps 5 and 6 then block a tracker verdict or a beacon instead of flagging it.
5. **Cached AI verdict:** needed → `ALLOW`; tracker → `FLAG` as a likely tracker; unsure → `FLAG`. The AI never blocks on its own: on real traffic the local model called Google's static files, Apple's update servers and Safe Browsing trackers at 0.8–0.9 confidence. A user rule ("block it"), a list or the keyword heuristic does the blocking.
6. **Beacon:** an unknown domain the device looks up on a regular timer (gap coefficient of variation ≤ 0.15, see `eddie/beacons.py`) → `FLAG`, source `heuristic`, reason "looked up every 60 s, likely telemetry"; still queued for AI.
7. **Unknown:** `ALLOW` + `FLAG` now, queue for AI.

### Classifier prompt contract

Input: domain, device label, query count in the last 10 min, whether the device was idle (no allowed media traffic in 5 min). Output strict JSON:

```json
{"label": "tracker|needed|unsure", "confidence": 0-1, "reason": "<15 words"}
```

Use Ollama `format: json`, temperature 0. Never send anything but the domain and these counts.

### REST API (localhost:8000)

Writes from anywhere but loopback need `Authorization: Bearer $EDDIE_API_TOKEN`, and `/ws/events` refuses browser pages from other sites (#96, docs/EVENTS.md). Reads are open.

| Method | Path | Returns / does |
|---|---|---|
| GET | `/devices?hours=24` | Devices Eddie has seen, with name, vendor, lookup / blocked / flagged counts, distinct trackers blocked and the latest blocked domains (see docs/EVENTS.md) |
| PUT | `/devices/{mac}` | `{label}` names a device ("Fire Stick"); `""` or `null` clears it |
| GET | `/events?since=&device=` | Recent query rows |
| GET | `/summary?window=1h` | Totals: queries, blocked, top blocked domains, top companies, per device |
| GET | `/what-they-know?hours=24` | "What they know about you" facts `{fact, evidence, confidence}` + spoken text, from `eddie.db` (else the in-memory feed); `?demo=true` returns the labelled seeded demo week instead (see docs/EVENTS.md) |
| GET | `/burglars-eye?hours=168` | Burglar's-eye report: when the house looked empty, devices ranked by occupancy leak score, security devices flagged `never_block`, and whether the data covered the range asked for; `?demo=true` uses the demo week (see docs/EVENTS.md) |
| GET | `/rules` | Every rule, newest first |
| POST | `/rules` | `{pattern, action, device_mac?, created_by?}` add rule; answers with the stored id and `applied` once DNS has loaded it (see docs/EVENTS.md) |
| DELETE | `/rules/{id}` | Remove rule; answers with the removed rule and `applied` |
| GET | `/mode` | `{home_mode, offline, offline_applied, away, away_until, away_applied, away_devices, away_exempt, quarantine, quarantine_applied}` |
| POST | `/mode` | `{home_mode?, away?, away_until?}`. Away mode ranks devices by occupancy leak, stores the covered and exempt ones, and answers with `away_applied` once DNS has loaded it (see docs/EVENTS.md) |
| POST | `/quarantine` | `{quarantine: bool}` holds new devices to the essentials until approved; answers with `applied` once DNS has loaded it |
| POST | `/devices/{mac}/approve`, `/devices/{mac}/deny` | Decide about a new device; answers with `status` and `applied` (see docs/EVENTS.md) |
| POST | `/devices/new` | New device alerts from the DNS server, pushed to `/ws/events` as `new_device` |
| POST | `/offline-mode` | `{offline: bool}` stops (or restarts) forwarding to upstream; allowed lookups get cached answers or SERVFAIL. Answers with `applied` once DNS has loaded it (see docs/EVENTS.md) |
| WS | `/ws/events` | Pushes events typed `query`, `ai_verdict`, `voice_state` or `agent_reply` |

### Agent tools (Ollama tool calling)

| Tool | Args | Effect |
|---|---|---|
| `get_summary` | window, device? | Reads `/summary?window=` per-device counts (implemented in `voice/ollama_agent.py`) |
| `get_recent_blocks` | device?, limit | Last blocked domains with reasons (implemented) |
| `what_they_know` | hours? | Reads `/what-they-know`; explicit ranges such as "the last 3 days" are honored, otherwise it uses all retained history, and the local model phrases checked inferences |
| `burglars_eye` | hours? | Reads `/burglars-eye` (implemented in `voice/voice_agent.py`); "when could someone tell the house was empty?" speaks its text, using the range the question names |
| `block` | pattern, device? | Adds BLOCK rule |
| `allow` | pattern, device? | Adds ALLOW rule |
| `lockdown_device` | device, keep (list) | Blocks everything for that device except keep patterns |
| `explain_domain` | domain | Returns stored verdict and reason (implemented) |
| `draft_email` | to, subject, body | Prepares email, asks for spoken yes |
| `send_email` | draft_id | Sends via Gmail only after confirmation |

**System prompt:** "You are Eddie, a local privacy guard. Answer in two short sentences, spoken style, a little dry. Use tools for facts; never invent numbers." Write tools never run inside the model turn: Eddie checks the call against the tool's schema, reads the change back and runs it only after a spoken yes.

### Voice loop

openWakeWord listens on the default mic (custom "hey eddie" model, or the built-in "hey jarvis" as fallback) → play chime, push `voice_state: listening` → record until 800 ms of silence (max 10 s) → faster-whisper base.en → agent → Piper speaks reply, push `agent_reply` to screen → back to listening. Mute the mic while Eddie speaks.

## Agent build spec: repo, build order, tests

### Repo layout

```
eddie/
  README.md            setup, sudo steps, demo script
  pyproject.toml
  eddie/
    dns_server.py      dnslib resolver, forward/sinkhole
    feed.py            sends each decision to the live feed (/ingest)
    decision.py        rule order, lists, heuristics
    classifier.py      Ollama queue worker
    devices.py         ARP/DHCP → device labels
    store.py           SQLite access
    api.py             FastAPI + WebSocket
    agent.py           tool-calling loop
    voice.py           wake word, STT, TTS
    email_tool.py      Gmail draft/send
    config.py          interfaces, ports, upstream, model name
  blocklists/ allowlist.txt
  dashboard/ index.html app.js style.css
  scripts/ setup_mac.sh run_all.sh fake_traffic.py
  tests/
```

- `fake_traffic.py` replays a recorded list of Fire Stick domains against the DNS server, so the dashboard and AI can be built before the hardware arrives at 5 PM.
- `fake_camera.py` (scripts/) is the simulated cloud camera for the home mode demo: webcam frame every few seconds, fresh DNS lookup through Eddie before each upload, posts `camera_upload` or `camera_blocked` events to the dashboard.

### Build order (24 hours)

1. DNS server on the Mac; point a test device's DNS at it and see lookups resolve through `dns_server.py` (Will, first hours). Move to the Pi after 5 PM.
2. Decision engine + SQLite logging + blocklists; Fire Stick still streams Netflix.
3. FastAPI + WebSocket + dashboard live feed (Adi, in parallel against `fake_traffic.py`).
4. Classifier worker with cached verdicts.
5. Checkpoint Saturday ~6 PM: real Fire Stick traffic on the live screen.
6. Agent with tools via text input first, then voice loop on top.
7. Offline mode toggle and the unplug demo.
8. Email action (only if 1–7 are solid).
9. Sunday morning: polish, rehearse, record backup video.

### Acceptance tests

- [ ] A device with its DNS set to Eddie gets every answer from Eddie (verify with `dig @<Eddie IP> google.com`).
- [ ] A blocklisted domain returns `0.0.0.0` within 20 ms; an allowed one returns the real IP.
- [ ] Fire Stick plays Netflix with blocking on.
- [ ] Each query appears on the dashboard within 1 second.
- [ ] Unknown domain gets an AI verdict within 10 seconds and is cached.
- [ ] "Hey Eddie, what's been going on?" returns a spoken summary with real counts.
- [ ] "Block it" adds a rule and the next query is blocked.
- [ ] With upstream unplugged, wake word, transcription, agent and dashboard still work.
- [ ] Email sends only after spoken confirmation.

### Guardrails for the agent building it

- No cloud APIs for speech or LLM. Only Gmail, only on request.
- Never log anything beyond domain, device, time, qtype, action, source and reason; keep at most `EDDIE_RETENTION_DAYS` (7) of history. See [docs/PRIVACY.md](docs/PRIVACY.md).
- Keep DNS hot path free of model calls.
- Dashboard style: black background, thin white type, one red accent for blocks, no clutter.

### Post-hackathon

Rewrite `dns_server.py`, `decision.py` and the traffic monitor in Rust on Tokio (hickory-dns), exposing the same REST/WebSocket API; Python keeps the agent and voice.

## Intelligence features (AI + algorithms)

**Principle:** code finds the facts, the local model explains them. The model never sees raw logs and never invents numbers. All reasoning stays on the box, because the inferences themselves are a profile of the household.

### Priority 1: "What they know about you"

The model reads a day of facts and writes what a company (or a burglar) could infer: "you're home after 6 PM", "someone watches TV past 1 AM", "a kids' app runs on weekend mornings", "the house is empty weekdays 8 to 6".

1. Label domains with a hand-written table (streaming, kids, music, telemetry, ads, camera, doorbell, thermostat).
2. Group requests into sessions: same category, gaps under 10 minutes.
3. Compute facts in Python: first and last activity per day, after-midnight sessions, category by weekday, idle telemetry, longest gap with no human activity.
4. Pass only a JSON list of `{fact, evidence, confidence}` to the model.
5. Prompt: "Using only these facts, write at most 5 things someone could infer. Phrase each as 'someone could guess…', cite the evidence, say how sure you are. Never add facts."
6. Validate in code: every time or number in the output must appear in the facts, or the line is dropped.
7. Demo: a live card from the current session, plus a clearly labelled week of seeded demo data for the full report.

### Priority 2: Plain-English rules

"The TV can only talk to Netflix", "lock down the kids' tablet", "I'm away this weekend" become real rules.

1. Model turns the sentence into tool calls: `lockdown_device(device, keep=[...])`, `block(pattern)`, `allow(pattern)`, `set_mode("away")`.
2. Eddie reads back what he'll do ("I'll block everything from the TV except Netflix and Amazon sign-in. OK?") and waits for yes.
3. Rules land in SQLite tagged `created_by: voice` so they can be listed and undone ("Hey Eddie, undo that").
4. Always keep an essentials allowlist (sign-in, updates, time) so a rule can't brick a device.

### Detection algorithms (feed both priorities)

- **Beacon detection:** for each device and domain, measure gaps between requests; a low coefficient of variation (very regular, e.g. every 30 or 60 seconds) means telemetry. Catches trackers not on any list.
- **Idle detection:** requests while there's no media traffic for 5 minutes are almost always tracking.
- **Device baselines:** learn each device's normal domains in its first hour; alert on anything new.
- **Company mapping:** group domains by owner (public tracker-ownership list) so the screen says "Amazon contacted you 312 times".
- **Evidence-based classification:** the model gets timing, idle behaviour and owner, not just the name, and reasons like an analyst.
- **Self-correcting blocks:** block, watch for errors or frantic retries, roll back if the device broke.

### Protecting the most valuable data: home occupancy

The most dangerous thing smart devices leak isn't what you watch. It's when you're home. Traffic from TVs, thermostats, smart plugs and doorbells rises and falls with people in the house, so anyone who gets that data (a breach, a broker, a compromised app) can tell when the place is empty.

- **Burglar's-eye report (built, #34):** a version of "What they know about you" that answers one question: "From recent data, when could someone tell the house was empty?" (`GET /burglars-eye`, voice: "when could someone tell the house was empty?"). It reads the range asked for (a week by default, or "the last 3 days", "today"; capped at retention with a notice); when Eddie hasn't been watching that long it says the answer is from recent data and how much, and when there's too little to conclude anything it says so. Empty times are waking hours with no streaming on most days Eddie saw, split into weekdays and weekends; DNS can't tell empty from asleep. Devices are ranked by leak score.
- **Occupancy leak score per device (built, #34):** 0 to 100, how differently a device's traffic behaves in hours someone was streaming vs hours nobody was. Highest scores get blocked or quieted first (blocking from the report is not built).
- **Away mode (#109, built):** "Hey Eddie, I'm leaving for the weekend." Eddie cuts non-essential telemetry from devices that reveal presence, so their traffic stops showing the house is empty. Built with an interim leak score (hours of streaming or traffic bursts this week) until #34; blocks labelled telemetry, AI-flagged trackers and beacons for those devices only, never security devices. See docs/EVENTS.md.
- **Security devices stay up:** alarm systems, cameras and doorbells keep talking to their own service in every mode. Eddie never blocks the thing protecting the house; he only blocks third parties around it.
- **Camera and doorbell watch:** alert if a camera starts uploading to a destination it has never used, or uploads spike at an odd hour.
- **Home mode for cameras (Ring and other indoor cams):** stops anyone outside the house (an employee, a hacker, the company) from seeing you while you're home. Eddie detects a household phone on the home Wi-Fi (seen in the last 10 minutes, matched by its per-network address) and blocks the indoor cameras' cloud upload domains (for Ring, its AWS-hosted endpoints). The camera app then shows those cameras as offline, which is intended: the video can't leave the house, and you don't need to watch your own living room. The moment the last household phone leaves, Eddie unblocks the domains and the cameras show up in the app again (e.g. to check on the dog). Override any time: "Hey Eddie, turn off home mode" or the dashboard toggle. Outdoor cameras, doorbells and alarms are never blocked. Motivated by the FTC's 2023 Ring case, where employees could view customer videos, including indoor cameras. Demo it on the Fire Stick ("the TV can't phone home while you're home") and pitch cameras as the main use case.
- **Simulated cloud camera (demo stand-in for Ring):** no Ring camera needed. Home mode is a rule that blocks a seeded list of camera upload domains (for Ring, a broad rule like `*.ring.com`, since exact upload domains aren't confirmed; the real product learns each camera's domains by watching its traffic). `fake_camera.py` runs on Will's laptop with its DNS pointed at Eddie: it grabs a webcam frame every few seconds and, before each "upload", does a fresh DNS lookup for a camera domain through Eddie. The dashboard shows "Living room cam: uploading" with the live frame; when Adi's phone is on the Wi-Fi, the lookup is blocked and the screen flips to "Upload blocked: home mode"; phone off the Wi-Fi → uploads resume. Call it a simulated camera in the pitch.
- **Streaming caveat:** blocking DNS doesn't cut a stream that's already running, because the device already knows the server's address. The fake camera re-looks up before every upload, so the demo is reliable. Roadmap: as the router, the real Eddie can cut open connections immediately with a firewall rule, not just block new lookups. If testing in a browser instead, turn off the browser's secure DNS (Chrome: Settings → Privacy → Use secure DNS) or it skips Eddie.
- **Camera account hardening coach:** walks you through two-factor login, end-to-end encryption where the camera offers it, and removing old shared users and logged-in devices. Eddie can't stop someone logging into your camera account from elsewhere, so this covers that gap.
- **New device alert (#98, implemented):** "A device I've never seen joined your network at 2:14 AM." With quarantine on (opt-in), a new device gets only Eddie's essentials until you approve it on the dashboard or say "allow the new device". DNS-only for now: a device with hard-coded IPs still gets out until router mode (#22/#23).
- **Presence masking (stretch):** in away mode, keep presence-revealing devices at a steady, normal-looking traffic level so the pattern looks occupied.

### Home mode demo with a real stream (Option A, planned)

Not built yet. Goal: show the audience what would happen to a Ring camera, using a real camera uploading to a real cloud, cut mid-stream by "Hey Eddie, turn on home mode". No fake script.

- **Why it matches Ring:** a Ring camera uploads straight to the cloud. Here a phone streams straight to YouTube Live, so Eddie cuts the camera itself, not a laptop in the house.
- **Eddie must be the router.** A live stream is one long connection, so DNS blocking can't stop one that's already running. The Pi's built-in Wi-Fi broadcasts an "Eddie" network that every device joins; the USB Wi-Fi adapter (keep it) joins the iPhone hotspot as the upstream. Eddie hands out addresses, answers DNS and runs the firewall (nftables). Side benefits: no per-device DNS setup, and phone presence comes straight from devices joining and leaving.
- **Camera:** a spare phone on Eddie's Wi-Fi running an RTMP app (e.g. Larix Broadcaster) with the YouTube stream key. Set the stream to "Ultra-low latency" so the cut shows in seconds, not 20–30.
- **Enforcement:** home mode adds the camera's IP to a firewall set; every packet to or from it is dropped, so the upload dies on the next packet. Turning home mode off removes it and the app reconnects.

  ```bash
  # one-time setup on the Pi
  sudo nft add table inet eddie
  sudo nft add set inet eddie homeblock '{ type ipv4_addr; }'
  sudo nft add chain inet eddie fwd '{ type filter hook forward priority 0; }'
  sudo nft add rule inet eddie fwd ip saddr @homeblock drop
  sudo nft add rule inet eddie fwd ip daddr @homeblock drop
  # home mode on / off (backend runs these with the camera's IP from the device table)
  sudo nft add element inet eddie homeblock '{ 10.42.0.23 }'
  sudo nft delete element inet eddie homeblock '{ 10.42.0.23 }'
  ```

- **Demo:** YouTube stream live on screen → "Hey Eddie, turn on home mode" (or Will's phone joins Eddie's Wi-Fi) → dashboard shows "Home mode: blocking Living room cam" → the stream stalls → home mode off → the stream comes back. The Fire Stick keeps playing Netflix next to it, trackers blocked.
- **Check early:** enable live streaming on the YouTube account now (first-time activation can take up to 24 hours); streaming through an app with a stream key avoids the YouTube mobile app's subscriber requirement. Test the Pi access point as soon as the kit arrives, since it's the riskiest step.
- **Fallback:** if router mode fails, stay on DNS-only, turn home mode on before the camera app reconnects (restart the stream) so the lookup is blocked, and present instant cut-off as what router mode does.
- Record the full sequence once it works, as the backup video.

### More ideas

- **Bypass detection:** a lookup of a known encrypted DNS resolver name now raises `bypass_suspected` and `EDDIE_BLOCK_DOH=on` blocks those names (#97). Still open: flagging devices that go quiet on DNS while clearly active, and spotting plain DNS sent to another resolver (needs router mode).
- **Privacy grade per device (A to F):** phone-home rate, idle activity, number of tracking companies, occupancy leak score, how much could be blocked without breakage.
- **Settings coach:** for each device, tells you which built-in privacy toggles to switch off (e.g. Fire TV usage data, interest-based ads), so the choice sticks at the source too.
- **Data broker watch:** highlight traffic to known data brokers and ad exchanges separately; these are where data gets sold on.
- **Night report:** a short spoken summary each morning: what was blocked, anything new, any occupancy leak.

### Build order

1. Domain labels, sessions, beacon and idle detection (needed by everything).
2. Home mode and the "What they know about you" card (both priority), then the burglar's-eye version.
3. Plain-English rules with read-back, plus away mode.
4. Company mapping and privacy grade.
5. Self-correcting blocks, new device alert, camera watch.
6. Presence masking only if everything else is solid.

## Beyond break-ins: the full map of what devices leak

Break-ins are one category. Eddie's "What they know about you" report groups every inference into these categories, each with a plain-English line, the evidence, and what Eddie did about it.

| Category | What it reveals | Signal Eddie sees | What Eddie does |
|---|---|---|---|
| Safety | When the house is empty, travel, routines | Activity gaps, away periods, device on/off times | Away mode, burglar's-eye report, presence masking |
| Health | Sleep schedule, insomnia, health apps, pharmacy and telehealth use | Late-night sessions, wake-up activity, health and pharmacy domains | Flag as sensitive, block third-party trackers inside health apps |
| Kids | Children in the home, their ages, their routines | Kids' apps and content, weekend and after-school patterns | Kid mode: strict lockdown of children's devices; kids' data is legally protected, so this is a strong pitch |
| Money | Shopping habits, payday timing, financial stress | Retail and fintech domains, spending-time patterns, loan and credit sites | Block ad and broker traffic around shopping apps |
| Household | How many people live there, when guests visit, who is home when | Number of active devices, new phones appearing, overlapping sessions | Guest network isolation, new device alerts |
| Work | Work-from-home schedule, employer, job searching | Work-tool domains, weekday daytime patterns, job sites | Keep work traffic separate; flag third-party trackers on job sites |
| Beliefs | Politics, religion, news diet, interests | News, religious and political content domains | Mark as sensitive; never stored beyond the day; block ad trackers there first |
| What's on screen | Everything shown on the TV, including other inputs | Automatic content recognition (ACR) traffic: regular screenshot-style fingerprints sent to ACR servers | Detect and block ACR endpoints; this is one of the biggest smart-TV leaks |
| Listening | Whether a speaker or TV mic sends audio | Upload bursts from a smart speaker when nobody said its wake word | Mic watch: alert on unexpected upload bursts |
| Identity linking | Your TV, phone and laptop tied together as one person | The same ad and analytics companies across multiple devices, ad ID syncing domains | Cross-device tracker map: "Google sees 4 of your 6 devices"; block the sync domains |

### Features that come out of this

- **Sensitivity modes:** "Hey Eddie, protect anything health related" blocks third-party trackers for that whole category, not one domain at a time.
- **Category privacy score:** the household gets a score per category (Safety B, Health D, Kids A) instead of one number, so people see what's actually exposed.
- **ACR blocker:** a dedicated check for smart-TV content recognition, shown as "Your TV was sending what's on screen every 15 seconds. Blocked."
- **Mic watch:** correlate speaker uploads with whether anyone spoke; flag uploads with no wake word.
- **Cross-device map:** a visual of which companies see which of your devices, and how many devices each company can link.
- **Data broker spotlight:** separate counter for known data brokers and ad exchanges, since that's where data gets sold on.
- **Forget timer:** sensitive categories (health, beliefs, kids) are summarised and the raw logs deleted after 24 hours, so Eddie never becomes the leak.

### For the demo

With a Fire Stick you'll mainly see Safety, What's on screen, Identity linking and some Household and Kids signals (if you open a kids' app). Show those live, and show the other categories on the seeded week of demo data, clearly labelled.


## Voice implementation update

The offline wake/capture/transcription worker and deterministic summary/home-mode tool router are implemented. Whisper, shared wake features, Vosk and Piper Lessac assets are pinned by SHA-256 in `voice/assets.json` and installed with `python scripts/provision_voice.py` (#39), which also runs a hardware check; `scripts/run_voice.sh` verifies them offline before starting. The dedicated Hey Eddie ONNX model still needs training; the explicit offline Vosk prototype is available now via `./scripts/run_voice.sh`. Live-room microphone accuracy remains unverified. Ollama tool calling (#29: `get_summary`, `get_recent_blocks`, `explain_domain`, `what_they_know`) answers questions, with every spoken number checked against tool results. Plain-English rules (#108) give the model `block`, `allow`, `lockdown_device` and `set_mode`, but a call only becomes a schema-checked read-back waiting for a spoken yes; email is not connected to the model. The keyword router handles block, allow, undo and "lock down <device>" (#17), and service allows and lockdown wait for a spoken yes (one pending action, 15 s expiry, discarded on dismissal); all are unit tested. See [voice setup](voice/README.md), [architecture and remaining work](docs/ARCHITECTURE.md), and [contributor instructions](AGENTS.md). The Mac is the development host; final deployment stays portable to Linux. Away mode (#109) is a keyword command too, and waits for a spoken yes to turn on or off.

The voice view now features a larger active Eddie, spoken interaction without a text box, and an Open logs control beneath Eddie that opens a drawer for recent network activity.

Voice conversations remain active between requests without repeating Hey Eddie. Four seconds waiting without speech dismisses Eddie, including immediately after the initial wake.
