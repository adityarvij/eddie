# Eddie architecture: voice implementation status

The backend API and DNS enforcement are independent of the voice worker. Development currently uses the Mac microphone and a localhost dashboard; production should run the same offline worker on the final box after ARM64 performance checks.

Audio → openWakeWord custom ONNX → bounded in-memory capture → faster-whisper local model → deterministic tool router → local REST API → Piper → audio output.

The dashboard bridge publishes `voice_status`, `wake`, `voice_state`, `transcript`, `agent_reply`, `speaking`, and `speech_end` events through its existing SSE connection. It accepts start, stop and typed ask requests. Speech and typed responses share a serialized lock. Recognition discards input during replies and resets detector history before rearming.

Implemented tools are backend summary reads, “what they know” inference reads (`what_they_know` → `GET /what-they-know`) and home-mode writes. Inferences are a scaffold: deterministic facts from the event log, templated into speech; the Ollama phrasing step and its number validation are pending. Home mode is backend state, not evidence of DNS enforcement. Missing tools explicitly report that no change occurred. The model does not fabricate counts. Ollama tool calling is a future layer over the same tool boundary; use structured validated arguments, bounded iterations and confirmation state for lockdown/email.

## Confirmation state

Risky tools go through a single pending-action slot in `voice/voice_agent.py` (`Confirmation`). A command only creates `{kind, args, prompt, expires_at}` and speaks a read-back; the action runs only when the next reply is an exact yes phrase within 15 seconds. No, cancel, a second unclear reply, expiry, conversation dismissal and microphone stop or error all discard it; a new risky command replaces it and says so. Ambiguous input and timeouts never execute. The bridge emits `voice_state: confirming` (`kind`, `prompt`, `expires_in_ms`; no arguments) after the read-back so the screen can ask for yes or no. Actions are registered in `RISKY_ACTIONS` and parsed by `RISKY_COMMANDS`; both are empty until lockdown and email exist, so this is implemented and unit tested but not yet reachable by voice. See [voice/README.md](../voice/README.md#confirmation-for-risky-actions) for the reply table.

## DNS server status

`eddie/dns_server.py` answers UDP DNS on port 53 with asyncio and dnslib. Each query goes through `decision.evaluate(domain, client_ip)`. Blocked queries get `0.0.0.0` / `::` (TTL 60, empty answer for other types). Allowed queries are forwarded as raw packets to each upstream in turn (2 s timeout each), and the answer is returned unchanged. If every upstream fails, the device gets SERVFAIL. Non-DNS packets are dropped, and at most 512 lookups are in flight. The decision path makes no model or network calls. Not built yet: TCP (truncated answers can't be retried over TCP), device labels (#14), and events to the live feed (#16).

`eddie/decision.py` applies README "Decision order" from in-memory snapshots only (no disk, model or network on the hot path; median about 4 µs a lookup on the development Mac with 150k blocklist entries and 1,000 rules). Writers replace whole snapshots: `set_lists` (#8 will load `blocklists/` and `allowlist.txt`), `set_rules` and `load_from_store` (rules and `ai_verdicts` from `eddie.db`). List entries cover their subdomains; a rule pattern `a.com` is exact and `*.a.com` covers a.com and its subdomains, with the most specific rule winning, then the newest. The short keywords `ads` and `log`/`logs` must be a whole word in a label so `uploads`, `blog` and `login` aren't blocked. Unknown domains are flagged and forwarded. The DNS server's `StoreWatcher` checks `eddie.db` every 0.2 s in a worker thread (a cheap count and max-timestamp query on `rules` and `ai_verdicts`, plus the `rules_rev` counter) and calls `load_from_store` only when one changed, so rules and verdicts written by other processes take effect within about 0.2 s. After each reload it writes the rules revision it loaded to `engine_state.rules_loaded`.

`eddie/rules.py` and the API's `/rules` endpoints (#12) are the rule writers. Each add or delete stores the row and bumps `engine_state.rules_rev` in one transaction, then the API waits up to 2 s for `rules_loaded` to reach that revision and returns `applied: true` only if it did. That is the DNS engine's acknowledgment, not just a stored row: once `applied` is true the very next lookup Eddie answers uses the change, and Eddie keeps no decision cache. Devices still cache earlier answers for their TTL (60 s for blocks, upstream's TTL for allowed answers), so a device may not ask again straight away. `applied: false` means the rule is stored but no running DNS server loaded it in time (DNS stopped, `EDDIE_DB` pointing at another file, or the API in Docker on a Mac while DNS runs on the host: the container has its own `eddie.db`). Voice must say so rather than claim the block worked. Lists aren't loaded until #8.

`eddie/classifier.py` runs as its own process. There is no separate queue or broker: the flagged `queries` rows are the queue. Each pass takes up to 10 domains looked up in the last hour that have no verdict, most recent first, builds the contract's input (domain, device label from `devices` or null, lookups in the last 10 minutes, and idle: no allowlisted traffic from that device in 5 minutes, or null if it has had none in the last hour), and calls Ollama `/api/chat` with `format: json` and temperature 0. Only an answer that is exactly `{label, confidence, reason}` with a known label, a number from 0 to 1 and a printable reason under 15 words is stored; anything else is dropped and that domain is retried after 5 minutes. When Ollama is unreachable the worker waits 10 s and tries again, without backing off domains. The prompt treats lookup counts and idle as weak hints; an earlier version that weighted them heavily labelled `api.netflix.com` a tracker.

`eddie/store.py` owns `eddie.db` (path from `EDDIE_DB`) and creates the README schema on first open (WAL mode, so readers don't block the writer). Every parsed DNS question writes one `queries` row with only ts (ms, arrival), device (client IP until #14), domain, qtype, action, source and reason; SERVFAILs are logged with the decision that was made, and non-DNS or question-less packets are not logged. The DNS task only puts the row on a bounded in-memory queue (10,000 rows); one writer thread batches inserts. If the writer falls behind, rows are dropped and counted rather than slowing DNS. Ctrl-C and SIGTERM flush the queue before exit; a hard kill loses at most the unflushed rows. Privacy measures: rows older than `EDDIE_RETENTION_DAYS` (default 7) are pruned at start and hourly with `secure_delete` plus a WAL truncate; the database files are `0600`; `EDDIE_DB=off` disables the log. Nothing reads the table yet: the API still keeps its own in-memory feed. Every privacy decision and known gap is tracked in [PRIVACY.md](PRIVACY.md).

## Remaining work

- Train and evaluate the actual Hey Eddie model; record provenance, license, threshold and false-wake metrics.
- Provision and hash all local assets; add a repeatable offline installer and hardware compatibility checks.
- Wire voice block / allow / undo to the `/rules` contract (docs/EVENTS.md): read back before writing, speak success only on `applied: true`, undo by deleting the returned id. Expiry is not built.
- Add authenticated local control before allowing dashboard controls off localhost. `serve.py --host 0.0.0.0` exposes only viewing (page, event stream, status); every POST control is refused unless it comes from a loopback address. Do not expose microphone controls publicly.
- Add calibrated VAD for noisy rooms; RMS endpointing is the first implementation. Consider an in-memory pre-roll for commands spoken without a pause.
- Add an audible ready cue without contaminating captured commands; currently the visual listening overlay is the cue.
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
