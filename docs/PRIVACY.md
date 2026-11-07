# Eddie privacy decisions (demo build)

Eddie watches a household's network, so Eddie itself holds the most sensitive data in the house: which device talked to which company, and when. This page records every privacy decision made for the hackathon demo. Each one is marked **implemented** (in code, tested where noted), **planned** (in the README or a ticket, not built), or **gap** (a known way we fall short today). Keep it current when any of these change.

**Principle:** Eddie sees where data goes, not what's inside it. Everything is processed on the box. Nothing about the household is sent anywhere, except what's listed under [What leaves the house](#what-leaves-the-house).

## Query log database (`eddie.db`)

Owner: [eddie/store.py](../eddie/store.py), written by [eddie/dns_server.py](../eddie/dns_server.py). Tests: [tests/test_store.py](../tests/test_store.py) and [tests/test_dns_server.py](../tests/test_dns_server.py).

| Decision | Status |
| --- | --- |
| History is stored on disk on the box, never in the cloud. The weekly report, per-device grades and undo all need it. | implemented |
| Each row holds only time (ms), device, domain, qtype, action, source and reason. No DNS answers, resolved IPs, packet bytes, URLs or page content. Schema is checked by a test. | implemented |
| **Retention:** rows older than `EDDIE_RETENTION_DAYS` (default **7**) are deleted when the server starts and every hour, including while the network is idle. Seven days is the weekly report's window and no longer. | implemented |
| **Deleted means deleted:** `PRAGMA secure_delete=ON` zeroes pruned rows' bytes, and the WAL is checkpointed and truncated after a prune so old pages don't linger there. | implemented |
| **Owner-only file:** `eddie.db`, `-wal` and `-shm` are mode `0600`; an existing looser file is tightened on start. | implemented |
| **Off switch:** `EDDIE_DB=off` logs nothing and creates no file. DNS filtering still works. | implemented |
| Logging never slows DNS. Rows go on a bounded in-memory queue (10,000), and a writer thread inserts them. If the disk stalls, rows are dropped and counted, never waited on. | implemented |
| `eddie.db*` is git- and docker-ignored. | implemented |
| The device column holds the client IP until #14 maps it to a MAC address and label. | gap |
| The file is not encrypted at rest. Anyone with root, or the SD card, can read up to 7 days of history. | gap |
| A hard kill (`kill -9`, power loss) loses queued rows. That's safe for privacy but leaves a hole in the history. Ctrl-C and SIGTERM flush first. | known behaviour |
| **Forget timer:** health, beliefs and kids categories would be summarised and their raw rows deleted after 24 hours ("never stored beyond the day"). Needs category labels on stored rows first. | planned |
| The `devices` table exists but nothing writes to it yet. | planned |
| `rules` holds only pattern, action, device (client IP until #14), scope, who created it and when. A deleted rule is deleted, not kept as history. Rules are user settings, so they are not pruned by `EDDIE_RETENTION_DAYS`. `engine_state` holds two counters (rules revision written and loaded) and nothing else. | implemented |
| `ai_verdicts` holds one row per domain (label, confidence, reason, time): no device, IP or count. It is pruned with the query log after `EDDIE_RETENTION_DAYS`, because the list of domains a household looked up is history even without times. A domain seen again is classified again. Tested. | implemented |
| **No new store for the classifier's queue.** It reads the flagged rows already in `queries`, so classification keeps nothing beyond the query log, and with `EDDIE_DB=off` nothing is classified at all. | implemented |

## Traffic API and live feed (port 8000)

| Decision | Status |
| --- | --- |
| The API keeps recent events in memory only (at most 20,000), so a restart wipes them. It does not read the `queries` table. | implemented |
| The DNS server sends every lookup (time, client IP, domain, qtype, action, source, reason: the same fields as a `queries` row) to `POST /ingest` over plain HTTP ([eddie/feed.py](../eddie/feed.py)). `EDDIE_API_URL` must be `localhost` or a loopback or private IP literal; public hosts and hostnames are refused at startup, redirects aren't followed and proxy settings are ignored, so lookups can't leave the house this way. Events wait only in a bounded in-memory queue (1,000) and are dropped, not stored, when the API is unreachable. Events are never written to the DNS server's log. `EDDIE_FEED=off` sends nothing. Tested in [tests/test_feed.py](../tests/test_feed.py). | implemented |
| With `EDDIE_API_URL` set to another machine's private IP, every lookup crosses the local network unencrypted and unauthenticated, readable by anyone on that network. The default keeps it on the box. | gap |
| Docker publishes the API on `127.0.0.1` only. `EDDIE_BIND=0.0.0.0` is an explicit opt-in to reach it from the iPad. | implemented |
| No authentication. With `EDDIE_BIND=0.0.0.0`, anyone on the network can read the feed, post fake events to `/ingest`, toggle `/mode`, and list, add or delete rules (`/rules`), including allowing a tracker or blocking a device's traffic. | gap |
| `/rules` reads and writes `eddie.db` (`EDDIE_DB`); it never reads the query log. Each stored add or delete is pushed to every `/ws/events` client as `rule_change` (pattern, action, device, who created it, when); it is not logged or kept in memory. With `EDDIE_DB=off` it answers 503 and stores nothing. In Docker the API uses a database inside the container, which is lost on rebuild and not shared with a DNS server on the host. | implemented |
| `/ws/events` does not check `Origin`, so any web page open on the same machine can read the live feed from `ws://localhost:8000`. | gap |
| uvicorn's default access log (request paths, client IPs) goes to Docker's container log. No request bodies are logged, so rule patterns aren't; a `DELETE /rules/{id}` path shows only the id. | known behaviour |
| The feed has no time-based retention; old events only drop out after 20,000 newer ones. | gap |

## Voice (wake word, speech to text, speech)

| Decision | Status |
| --- | --- |
| All wake detection, transcription and speech run locally. There is no cloud speech fallback. | implemented |
| Models are provisioned once at setup. At runtime Whisper uses `local_files_only`, Hugging Face telemetry is off, and Piper loads a local file. | implemented |
| Audio stays in memory and is bounded: a small pre-roll, at most 10 s per command, ended by 800 ms of silence. Nothing is written to disk. | implemented |
| Transcripts and replies are never stored or logged. | implemented |
| The mic ignores audio while Eddie speaks, so Eddie never transcribes itself. | implemented |
| The mic starts only on an explicit start request, not on page load, and stops on request. | implemented |
| A missing custom wake model is an error, never a silent switch to another engine. | implemented |
| **Prototype wake engine (Vosk, the dev default):** it transcribes all room speech locally to spot "hey Eddie", then throws the text away. That is broader listening than a dedicated wake model. | gap, until #28 trains openWakeWord |
| Transcripts are streamed live over the dashboard's `/events` SSE stream. Any local process can subscribe because GET has no origin check, though the text is never stored. | gap |
| A visible, persistent mic indicator. | planned |
| Asset hashing and an offline installer (#39). | planned |

## Dashboard and voice bridge (port 5173)

| Decision | Status |
| --- | --- |
| `frontend/serve.py` binds `127.0.0.1` only. | implemented |
| Cross-origin POSTs are rejected; bodies are capped at 64 KB and typed asks at 1,000 characters. | implemented |
| The page loads no external fonts, scripts, analytics or cloud audio. | implemented |
| Authenticated local control before exposing the bridge or mic controls beyond localhost (#40). Mic controls must never be public. | planned |

## Agent, facts and actions

| Decision | Status |
| --- | --- |
| Eddie never invents numbers. Summaries are computed in code from real events, and if the backend is down Eddie says so. | implemented |
| A network action is reported as done only after the backend echoes the new state. Home mode state alone doesn't prove DNS enforcement. | implemented |
| Voice commands that change settings must match the whole utterance, so negations or extra words don't trigger them. | implemented |
| Rules, lockdown and email are not wired up; Eddie says that nothing changed. | implemented |
| The local model gets only computed facts (`{fact, evidence, confidence}`), never raw logs. Every number in its output is checked against the facts. | planned |
| The classifier sends the model only the domain, the device's label (null until #14; never its MAC or IP), lookups in the last 10 minutes and an idle flag. Tested. | implemented |
| The classifier only talks to `localhost` or a loopback or private **IP literal**. Hostnames are refused, so DNS can't point it somewhere else; redirects aren't followed and `HTTP(S)_PROXY` is ignored. Tested. | implemented |
| Domains, model output and verdicts are never written to the classifier's log; it logs only counts and error types. Malformed model output is rejected, not stored. Tested. | implemented |
| Ollama itself may keep its own logs; its default doesn't log prompts, but `OLLAMA_DEBUG` would. | known behaviour |
| Email is sent only after spoken confirmation; lockdown needs confirmation too. | planned |
| Inferences ("what they know") stay on the box, because the inferences are themselves a profile of the household. | planned |

## Home mode and camera

| Decision | Status |
| --- | --- |
| Home mode is a backend flag. DNS doesn't enforce it yet (`decision.py` ignores it), and the docs say so. | implemented / gap |
| Phone presence detection, nftables enforcement, and never blocking security devices. | planned |
| The YouTube Live stream used in the home mode demo sends camera video to Google. It's a staged demo prop, not something Eddie does. | planned, demo only |

## Demo data

| Decision | Status |
| --- | --- |
| `scripts/fake_traffic.py` is scripted. Its devices, MAC address and verdicts (including the "AI:" ones) are invented, and it contains no real household data. | implemented |
| The seeded "demo week" for the full report must be clearly labelled as demo data. | planned |
| Eddie reads fake-traffic counts aloud the same way as real ones; the voice summary doesn't say which it is. | gap |

## What leaves the house

| Outbound | Carries | Status |
| --- | --- | --- |
| Upstream DNS (`1.1.1.1`, then `9.9.9.9`), plain UDP | Every **allowed** domain name, from Eddie's IP | implemented. Blocked lookups never leave. "Data never leaves the house" means device data and history, not the lookups we forward. Encrypted upstream (DoT/DoH) is not built. |
| Gmail API | The report email, only on request and after confirmation | planned |
| Tavily / Exa (optional) | One unknown domain name, never user data | planned, optional |
| Model downloads | Nothing about the household | setup only, never at runtime |

Ollama runs on the host and is local; the classifier refuses any non-private Ollama address, so the domains it sends stay on the home network. The setup checklist's `OLLAMA_HOST=0.0.0.0` exposes it, unauthenticated, to everyone on the hotspot. **Gap:** prefer a localhost bind when the Pi isn't calling it.

## Doc mismatches to fix

- README "Guardrails" said to log only domain, device, time and action. The schema also stores qtype, source and reason, which are needed for the feed and for undo. The guardrail now names those fields too.
- README "only Gmail" outbound claims leave out upstream DNS and the optional Tavily/Exa lookup. This page is the authoritative list.

### Company summaries (#25, implemented)

Ownership and public suffix data are bundled setup assets. Labeling makes no outbound requests and creates no cache files. Per-company counters are computed from the existing in-memory API events; no new browsing-history store or retention period is added. They count requested DNS lookups, not uploaded data. Unmapped domains are presented as registrable domains rather than guessed companies.


### Voice activity calibration (#38, implemented)

WebRTC VAD runs locally with no model downloads or remote calls. Calibration retains at most 125 scalar energy values in memory (ten seconds); commands retain the existing bounded audio pre-roll only. The ready tone is generated in memory. Capture is disabled during tone playback and its tail, then buffered audio is discarded. Tests use a licensed public speech fixture, not household recordings. No new audio, transcript, or calibration file is written.


### Classifier event delivery (#27, implemented)

The worker retains at most two in-flight inference inputs and 1,000 undelivered verdict events in memory. The verdict event carries only domain, label, confidence, short reason and timestamp, never device identity or traffic counts. Local API delivery ignores proxies, refuses public hosts and redirects, and retries failures without storing a new history log. Events are visible to existing live-feed subscribers under the API's existing access limitations; keep it bound to loopback until authentication is built. Worker shutdown/restart drops its in-memory notification outbox. Persistent verdict retention stays unchanged. Model reasons are untrusted display text, never HTML.


## Real AI on the scripted demo feed

`python -m eddie.feed_classifier` polls the local API for scripted demo queries and feeds the real bounded Ollama worker using an in-memory SQLite queue/cache. Real DNS still uses the durable database worker. `run_all.sh` launches the adapter only with the fake-traffic profile. Stopping it loses its cache, and no demo network history is added to disk. It polls up to 2,000 recent events once per second and retains at most 20,000 query rows for an hour; heavy traffic can outrun that window. Inference remains on the host with local-only URLs, not in the DNS or API request path.

The API retains at most 1,000 verdicts in memory, returns them from `GET /ai/verdicts`, and includes `ai_verdicts` in WebSocket snapshots so a browser refresh does not hide completed results. The fake generator uses these real cached verdicts for later decisions, while still labelling the traffic itself as scripted demo traffic in its launcher/docs. The old hard-coded “sent to AI” and fabricated AI confidence text are removed. Unknowns say “awaiting classification”; an `[ai]` line is printed only after actual model output is validated. These verdicts do not prove anything about real Fire Stick traffic.
