# Eddie architecture: voice implementation status

The backend API and DNS enforcement are independent of the voice worker. Development currently uses the Mac microphone and a localhost dashboard; production should run the same offline worker on the final box after ARM64 performance checks.

Audio → openWakeWord custom ONNX → bounded in-memory capture → faster-whisper local model → deterministic tool router → local REST API → Piper → audio output.

The dashboard bridge publishes `voice_status`, `wake`, `voice_state`, `transcript`, `agent_reply`, `speaking`, and `speech_end` events through its existing SSE connection. It accepts start, stop and typed ask requests. Speech and typed responses share a serialized lock. Recognition discards input during replies and resets detector history before rearming.

Implemented tools are backend summary reads, “what they know” inference reads (`what_they_know` → `GET /what-they-know`) and home-mode writes. Inferences are a scaffold: deterministic facts from the event log, templated into speech; the Ollama phrasing step and its number validation are pending. Home mode is backend state, not evidence of DNS enforcement. Missing tools explicitly report that no change occurred. The model does not fabricate counts. Ollama tool calling is a future layer over the same tool boundary; use structured validated arguments, bounded iterations and confirmation state for lockdown/email.

## Confirmation state

Risky tools go through a single pending-action slot in `voice/voice_agent.py` (`Confirmation`). A command only creates `{kind, args, prompt, expires_at}` and speaks a read-back; the action runs only when the next reply is an exact yes phrase within 15 seconds. No, cancel, a second unclear reply, expiry, conversation dismissal and microphone stop or error all discard it; a new risky command replaces it and says so. Ambiguous input and timeouts never execute. The bridge emits `voice_state: confirming` (`kind`, `prompt`, `expires_in_ms`; no arguments) after the read-back so the screen can ask for yes or no. Actions are registered in `RISKY_ACTIONS` and parsed by `RISKY_COMMANDS`; both are empty until lockdown and email exist, so this is implemented and unit tested but not yet reachable by voice. See [voice/README.md](../voice/README.md#confirmation-for-risky-actions) for the reply table.

## DNS server status

`eddie/dns_server.py` answers UDP DNS on port 53 with asyncio and dnslib. Each query goes through `decision.evaluate(domain, client_ip)`. Blocked queries get `0.0.0.0` / `::` (TTL 60, empty answer for other types). Allowed queries are forwarded as raw packets to each upstream in turn (2 s timeout each), and the answer is returned unchanged. If every upstream fails, the device gets SERVFAIL. Non-DNS packets are dropped, and at most 512 lookups are in flight. The decision path makes no model or network calls. Not built yet: TCP (truncated answers can't be retried over TCP), device labels (#14), and events to the live feed (#16).

`eddie/decision.py` applies README "Decision order" from in-memory snapshots only (no disk, model or network on the hot path; median about 4 µs a lookup on the development Mac with 150k blocklist entries and 1,000 rules). Writers replace whole snapshots: `set_lists` (#8 will load `blocklists/` and `allowlist.txt`), `set_rules` and `load_from_store` (rules and `ai_verdicts` from `eddie.db`; #12 must call it after each rule change). List entries cover their subdomains; a rule pattern `a.com` is exact and `*.a.com` covers a.com and its subdomains, with the most specific rule winning, then the newest. The short keywords `ads` and `log`/`logs` must be a whole word in a label so `uploads`, `blog` and `login` aren't blocked. Unknown domains are flagged and forwarded. The DNS server's `StoreWatcher` checks `eddie.db` once a second in a worker thread (a cheap count and max-timestamp query on `rules` and `ai_verdicts`) and calls `load_from_store` only when either changed, so rules and verdicts written by other processes take effect within about a second. Lists aren't loaded until #8.

`eddie/classifier.py` runs separately from DNS. Flagged query rows are its durable queue. The production loop schedules at most two HTTP inference requests concurrently, oldest observed first, and polls futures every 250 ms without waiting for a model. Each request has a seven-second socket timeout and bounded output tokens; load, backlog and cold starts can still miss the ten-second target. No model is called by DNS. Inputs are domain, optional friendly device label, ten-minute lookup count and idle (no allowed, labelled media traffic in five minutes; unknown without media history). Strict validated JSON is cached in `ai_verdicts`, then DNS's watcher reloads it. Invalid output backs off for five minutes; unavailable inference backs off for ten seconds. A bounded in-memory event outbox retries local API delivery without rerunning inference. Restart can lose undelivered events, but cached verdicts remain authoritative. The initial #27 implementation is retained through its synchronous compatibility interface and tests.

`eddie/store.py` owns `eddie.db` (path from `EDDIE_DB`) and creates the README schema on first open (WAL mode, so readers don't block the writer). Every parsed DNS question writes one `queries` row with only ts (ms, arrival), device (client IP until #14), domain, qtype, action, source and reason; SERVFAILs are logged with the decision that was made, and non-DNS or question-less packets are not logged. The DNS task only puts the row on a bounded in-memory queue (10,000 rows); one writer thread batches inserts. If the writer falls behind, rows are dropped and counted rather than slowing DNS. Ctrl-C and SIGTERM flush the queue before exit; a hard kill loses at most the unflushed rows. Privacy measures: rows older than `EDDIE_RETENTION_DAYS` (default 7) are pruned at start and hourly with `secure_delete` plus a WAL truncate; the database files are `0600`; `EDDIE_DB=off` disables the log. Nothing reads the table yet: the API still keeps its own in-memory feed. Every privacy decision and known gap is tracked in [PRIVACY.md](PRIVACY.md).

## Remaining work

- Train and evaluate the actual Hey Eddie model; record provenance, license, threshold and false-wake metrics.
- Provision and hash all local assets; add a repeatable offline installer and hardware compatibility checks.
- Integrate Will's rule endpoints only after their request/response contract exists; add read-back, expiry and undo semantics.
- Add authenticated local control before allowing dashboard controls off localhost. `serve.py --host 0.0.0.0` exposes only viewing (page, event stream, status); every POST control is refused unless it comes from a loopback address. Do not expose microphone controls publicly.
- Rehearse the calibrated WebRTC VAD on the actual speakerphone; adjust aggressiveness/noise margin for the room.
- Ready cue and capture suppression are implemented; test acoustic echo tails on final hardware.
- Add bounded request admission, cancellation, streaming replies, device reconnect and model caching for Piper.
- Add a visible persistent microphone indicator and shutdown path for deployment.
- Keep models/audio out of git. Never retain raw microphone audio or log transcriptions by default.

## Explicit prototype engine

`EDDIE_WAKE_ENGINE=vosk` selects local phrase recognition for immediate Mac testing; the normal default remains `openwakeword`. `wake_detector.py` isolates prototype phrase detection from bounded capture, Whisper, tools and Piper. The prototype uses an unrestricted decoder (no forced wake grammar), requires an anchored wake phrase, and advertises its engine through voice status. A generated training notebook is preparation only; no dedicated model is claimed.

The development launcher uses provisioned Piper Lessac rather than macOS speech. Existing backend traffic is preserved across dashboard restarts. `/api/voice/status` supports readiness checks without storing audio or transcriptions.

## Voice-first presentation

The assistant view has no text-entry UI. Its larger character stays animated while open, with distinct listening, thinking and speaking states. After a wake and after each reply the worker keeps listening for a follow-up; 4 seconds without speech emits `voice_state: dismissed`, which closes Eddie and returns to the live feed. A new wake phrase is then required. Open logs toggles a live recent-network-activity drawer inside the assistant view, using the existing feed rather than a new log store.

The assistant copy beneath the character is hidden in the voice view; state remains above Eddie and Open logs remains available. Speech events still drive the character animation.


Voice recovery: after an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again. Four seconds of silence after the initial wake or during a follow-up dismisses Eddie back to the newest feed activity. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.

Conversation activity is explicit (`is_eddie_active`): wake activates it, each spoken reply starts a fresh four-second wait, and silence dismisses it. The initial wait has no retry prompt or extra four-second grace period. Speaking and transcription do not count toward the user response window.

Dismissal fades and gently lowers Eddie over 320 ms before closing the dialog. Escape uses the same exit. A new wake cancels an unfinished exit; reduced-motion preferences skip the animation. The four-second response timeout starts the exit rather than closing abruptly.

The Open logs control sits directly beneath Eddie, away from the ambient edge glow; its live drawer remains anchored separately.

## Offline company ownership (#25)

`labels.py` preserves precise local category overrides, supplements owner names with 37 public Tracker Radar entries (source/license in `eddie/data/README.md`), and uses tldextract's bundled PSL for unknown registrable domains. Fetching and disk caching are disabled. Owner names alone never trigger blocking or imply tracking. `/summary` adds top-company request/block/tracking counts over the past hour. This view is bounded by the API's in-memory feed and is not an all-time database report.


## Voice activity and ready tone (#38)

The worker uses a local WebRTC speech gate plus an idle noise-energy estimate, rather than an RMS-only speech threshold. Only scalar calibration energies persist in a bounded deque. A generated 80 ms tone plays after wake; capture suppression covers playback plus 160 ms, and the queue/detector are drained before the listening state is published. The public recorded speech fixture mixed with generated noise and the full worker lifecycle pass automated checks. Live-room accuracy remains a hardware rehearsal task.


### Classifier live events and verification (#27)

The worker publishes validated metadata to local `POST /ai/verdict`; the API broadcasts `ai_verdict` on `/ws/events`. The frontend prints the verdict without counting it as another DNS lookup. The two-thread inference pool is bounded; further work stays in the existing query log. This verifies off-path execution and cache/event behavior, not AI correctness for every domain. The public `api.netflix.com` smoke test returned a valid `needed` verdict in 3.94 seconds on the development Mac; this is one measurement, not a latency guarantee under load. Provision Ollama/model before runtime; no model download is triggered.
