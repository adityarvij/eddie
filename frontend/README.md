# Eddie laptop interface

Run from the repository root:

```sh
python3.11 -m venv .venv-frontend
.venv-frontend/bin/pip install -r voice/requirements.txt
.venv-frontend/bin/python frontend/serve.py
```

Open http://localhost:5173. Press **Space** to preview the character. The footer has one button, **start mic** / **stop mic** (one or the other, never both), prefixed “offline •” while offline mode is confirmed on. Escape dismisses him. Mouth animation follows the duration of macOS local speech output. No browser speech-recognition service is used.

### Viewing from the iPad

`serve.py` listens on localhost only. To show the dashboard on another device on the same network:

```sh
.venv-frontend/bin/python frontend/serve.py --host 0.0.0.0   # or EDDIE_DASHBOARD_HOST=0.0.0.0
```

Then open `http://<Mac IP>:5173` on the iPad. Other devices can view the page, the event stream and every read-only GET endpoint. To pair a device for typed asks and the mode toggles, open the `http://<Mac IP>:5173/#pair=…` link `serve.py` prints at start (set `EDDIE_DASHBOARD_TOKEN` to keep the same link across restarts); the page sends the token on control requests. Microphone start/stop/dismiss and `/api/events` answer 403 unless they come from the computer running Eddie, paired or not, so the microphone can't be switched on from the network. See [ARCHITECTURE.md](../docs/ARCHITECTURE.md#dashboard-bridge-control-40). The traffic API must also accept network connections (`EDDIE_BIND=0.0.0.0` for the Docker API, or `--host 0.0.0.0` for uvicorn) and the macOS firewall must allow incoming connections for Python.

## Local “Hey Eddie”

The voice pipeline now uses openWakeWord and faster-whisper. Follow [offline model provisioning and voice setup](../voice/README.md) before enabling the microphone. Custom models are not bundled. Piper supplies portable speech; installed macOS speech remains a development fallback.

Supported tools: factual backend summaries and explicit home-mode on/off. Domain rules, lockdown, email and general agent conversation remain pending.

## Rules and confirmations (#19)

Under the counts, **rules** lists up to five active rules, newest first (`block device-metrics-us.amazon.com · Fire Stick · by voice`), from `GET /api/rules`, which `serve.py` relays read-only from the traffic API (`EDDIE_API_URL`, default `http://127.0.0.1:8000`) so the iPad needs no CORS or direct API access. It refreshes on connect and on every `rule_change` event, and says `unavailable` if the API or `eddie.db` is not reachable. The device panel (#35, below) sits beside it.

The page no longer has buttons for the report; `window.eddie.renderReport(report)` still shows a report panel above the feed with each fact, its evidence and confidence, from `GET /api/what-they-know` (live, last week, from `eddie.db` when the DNS server has logged lookups, else the in-memory feed; the title says “from eddie.db” in the first case) or `GET /api/what-they-know?demo=1`, both still relayed read-only by `serve.py` (voice uses the live report). The demo report always carries an inverted "DEMO DATA · …" banner and "DEMO WEEK" in its title; live reports never do.

Each `rule_change` adds a `[rule]` feed row and a toast, e.g. “Rule added: block device-metrics-us.amazon.com · Fire Stick · by voice”. The event is sent before DNS acknowledges the rule, so the dashboard says “added”, never “blocked”.

While voice is `confirming`, the overlay shows Eddie's read-back (“Lock down the TV?”) and “SAY YES TO CONFIRM OR NO TO CANCEL”. Both clear on the next reply, on dismissal (Escape, ✕, silence) and when `expires_in_ms` runs out. Nothing is hover-only and nothing is stored in the browser.

Manual check (needs the API and DNS server sharing one `eddie.db`, plus this bridge):

1. Open the dashboard on the Mac and on the iPad (kiosk / Add to Home Screen).
2. `curl -X POST localhost:8000/rules -H 'Content-Type: application/json' -d '{"pattern":"device-metrics-us.amazon.com","action":"block","created_by":"voice"}'`: both screens show the toast and `[rule]` row, and the list shows the rule.
3. `curl -X DELETE localhost:8000/rules/<id>`: “Rule removed” appears and the list drops it.
4. With a confirmation-gated command available, say it: the read-back and yes/no hint stay up through the follow-up listening; wait 15 s and they clear; repeat and press ✕, and they clear.
5. Reload the iPad page: the list comes back from the API; no toast, feed row or prompt is restored.

Not yet run on hardware: steps 1 to 5 on the iPad in kiosk mode. Step 4 can't be done by voice yet, because no spoken command reaches the confirmation slot until lockdown or email is wired; `node tests/test_frontend_events.cjs` covers expiry and dismissal.

## Devices and mode toggles (#35)

Home mode is a house icon right after the `eddie_` heading (outlined off, filled on); **offline mode** is a toggle under the counts; and beside the rules a **devices** list: each device's name (label, else DHCP hostname, else "<vendor> device", else its MAC or IP), how many distinct domains Eddie blocked for it, and its three latest blocked domains, for the last 24 hours, most blocked first, up to five devices then "+N more". The list comes from `GET /api/devices` and the toggles' state from `GET /api/mode`, both relayed read-only by `serve.py` from the traffic API (`/devices?hours=24`, `/mode`), so they need the API and `eddie.db` (`EDDIE_DB`). Both refresh on connect and every 30 seconds; the toggles also follow the API's `mode` and `offline_mode` broadcasts, so a change from voice or another screen shows up. If the API doesn't answer, the list says `unavailable` and the toggles are disabled (home mode's label, read by screen readers and shown on hover, says `unavailable`).

The toggles only ever show state the backend acknowledged:

- A tap shows the pending state (the house icon's outline turns dashed; the offline toggle reads `offline mode: turning on…` with a dashed border), disables the control, and sends `POST /api/mode` (`{home_mode}`) or `POST /api/offline-mode` (`{offline}`). The new state appears only when the API echoes it back. A second tap while pending sends nothing.
- If the API errors or doesn't answer within 10 seconds, a toast says "Home mode change not confirmed: …" and the toggle reloads its real state from the API.
- Offline mode that the API stored but the DNS server hasn't loaded (`applied: false`) shows in red: "offline mode on not yet applied by DNS". It clears when the DNS server picks it up.
- Home mode on always adds "home mode is a backend flag; DNS does not enforce it yet", because `decision.py` ignores it.

From the iPad the toggles need the pairing link (`#pair=…`); without it they answer 401 and the toast says to pair. Tap targets are 44 px; nothing is hover-only.

Manual check (needs the API and DNS server sharing one `eddie.db`, plus this bridge):

1. Open the dashboard on the Mac and on the paired iPad in Safari (kiosk / Add to Home Screen).
2. Browse on a device pointed at Eddie: within 30 s it appears with its tracker count and latest blocked domains. Label it (`curl -X PUT localhost:8000/devices/<mac> -H 'Content-Type: application/json' -d '{"label":"Fire Stick"}'`) and the name follows on the next refresh.
3. Tap offline mode on the iPad: it shows "turning on…", then "on" on both screens. Stop the DNS server and tap it off: it shows "off" with the red "not yet applied by DNS" line.
4. Tap home mode: the backend-flag note appears. Say "turn off home mode": both screens follow.
5. Stop the API: within 30 s the list and toggles say `unavailable`. Open the iPad page without the `#pair=` link: a tap gives the pairing toast and the toggle stays as it was.

Checked here: the automated tests (`node tests/test_frontend_events.cjs`, `tests/test_dashboard_bridge.py`, `tests/test_devices.py`), and the page in a desktop browser at 1024×768 and 375 px wide against a seeded `eddie.db` with no DNS server (offline mode went pending, then showed "not yet applied by DNS"). Not yet run on hardware: steps 1 to 5 on the real iPad in Safari kiosk mode.

## Traffic contract for Claude

The browser automatically reconnects to `ws://<page host>:8000/ws/events`: `localhost` on the Mac, the Mac's IP from the iPad. Override with `?traffic=ws://HOST:PORT/path`. Each message may be one event, an array, or `{ "events": [...] }`.

```json
{"timestamp":"2026-11-07T16:30:00Z","device":"Fire Stick","domain":"device-metrics-us.amazon.com","action":"block","reason":"Known telemetry","source":"blocklist"}
```

Alternative: POST the same JSON to `http://localhost:5173/api/events`. This route also supplies counts for the placeholder voice summaries. WebSocket traffic currently only populates dashboard counts. No sample traffic is generated by the frontend; label seeded/demo data at its source.

Voice lifecycle events may also arrive on the traffic WebSocket:

```json
{"type":"wake"}
{"type":"transcript","text":"What's going on?"}
{"type":"speaking","text":"I blocked three tracking requests."}
{"type":"speech_end"}
```

For a separate agent use the WebSocket events above to control the overlay, or call `window.eddie.wake()`, `window.eddie.pushEvent(event)`, and `window.eddie.voiceEvent(event)` in the page.

The server binds only to localhost for the laptop demo. External fonts, analytics, and cloud audio are not used.

## Voice view

Calling Eddie opens a larger animated character with listening, thinking and speaking states. After “Hey Eddie” and after each reply, Eddie keeps listening for a follow-up without the wake phrase. If nothing is said for 4 seconds, Eddie disappears and the live feed comes back; the next request starts with “Hey Eddie” again. A preview opened with **Wake Eddie** or **Space** while the microphone is off also closes after 4 seconds. There is no typed input. Transcripts and replies are not displayed beneath Eddie; speech is the primary response.

**Open logs** just below Eddie opens a live drawer of the same recent network activity shown by the main feed. Closing the drawer leaves Eddie open. Escape or the top close control dismisses Eddie. This drawer does not persist microphone audio or speech transcripts.


Voice recovery: after an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again, once; a second empty transcription in a row (usually background noise) ends the conversation silently. Four seconds of silence after the initial wake or during a follow-up dismisses Eddie back to the newest feed activity. Closing the overlay (×, Escape or a click outside) also ends the conversation in the voice worker (`POST /api/voice/dismiss`, localhost only), so the next request needs the wake phrase; a reply already being spoken finishes, but the overlay stays closed. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.

Conversation activity is explicit (`is_eddie_active`): wake activates it, each spoken reply starts a fresh four-second wait, and silence dismisses it. The initial wait has no retry prompt or extra four-second grace period. Speaking and transcription do not count toward the user response window.

Dismissal fades and gently lowers Eddie over 320 ms before closing the dialog. Escape uses the same exit. A new wake cancels an unfinished exit; reduced-motion preferences skip the animation. The four-second response timeout starts the exit rather than closing abruptly.
