# Offline voice setup

Run from the Eddie repository root with Python 3.11. The laptop is a development host; the audio worker is separate from DNS and can move to the final Linux box.

## Provision once while online

1. Install `voice/requirements.txt` in a dedicated virtual environment. On Linux install PortAudio (`libportaudio2`) if sounddevice needs it. Verify platform wheels before moving to Pi ARM64.
2. Run `python scripts/provision_voice.py` (#39). It downloads only what's missing from the pinned sources in `voice/assets.json` (Whisper base.en, Piper Lessac medium, the Vosk small prototype model and openWakeWord's shared melspectrogram/embedding ONNX files), checks each file's SHA-256, then runs the hardware check. Same command on the Mac and Linux/Pi; it uses only the standard library to fetch. Steps 4–6 below are what it automates.
   - A download whose hash or size doesn't match is deleted and the script exits 1 with `PROVISIONING FAILED`. An existing file with the wrong hash is never overwritten; delete it and run again.
   - `--verify` hashes every asset without using the network and exits 1 listing each missing or altered file. `scripts/run_voice.sh` runs it before launching, so a corrupt model fails before the microphone opens (about 0.3 s on the development Mac).
   - `--hardware` reports platform, Python version, required packages, whether the input device (`EDDIE_INPUT_DEVICE` or the default) accepts 16 kHz mono int16, the output device, and, on Linux, that `EDDIE_PIPER_MODEL` and its JSON exist. It opens no stream and records nothing, so it doesn't prove the microphone hears you.
   - The custom `hey_eddie.onnx` isn't in the manifest: it hasn't been trained (#28), so there's nothing to pin.
3. Train a custom **hey eddie** model using the official openWakeWord training notebook. Export ONNX to `voice/models/hey_eddie.onnx`. An arbitrary filename does not change what an existing model recognizes. Do not rename Hey Jarvis and present it as Hey Eddie.
3. Provision openWakeWord's shared embedding and mel-spectrogram ONNX assets in its package model directory using its setup utility (`openwakeword.utils.download_models()`) while online. The worker does not call a downloader.
4. Provision Whisper locally (pinned to revision `3d3d5de` in the manifest), for example with `huggingface_hub.snapshot_download('Systran/faster-whisper-base.en', local_dir='voice/models/whisper-base.en')`. This is a setup download, not a runtime speech API.
5. For portable speech, provision a licensed Piper `.onnx` voice and matching `.onnx.json` file, then set `EDDIE_PIPER_MODEL` to its absolute path. macOS development falls back to the installed `/usr/bin/say` voice when this variable is unset; Linux needs Piper configured.

References: [openWakeWord training](https://github.com/dscripka/openWakeWord#training-new-models), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Piper](https://github.com/OHF-Voice/piper1-gpl).

## Run

Start the traffic API separately on port 8000, then run `python frontend/serve.py` in the voice environment. Open http://localhost:5173 and enable the microphone. Grant microphone permission to the host application on macOS. Start/stop controls are explicit; loading a page does not start capture.

Configuration:

| Variable | Default / purpose |
|---|---|
| `EDDIE_WAKE_MODEL` | `voice/models/hey_eddie.onnx` |
| `EDDIE_WHISPER_MODEL` | `voice/models/whisper-base.en` directory; local files only |
| `EDDIE_WAKE_THRESHOLD` | `0.5`; two consecutive positive frames |
| `EDDIE_SPEECH_THRESHOLD` | `0.003` minimum normalized RMS floor; WebRTC speech votes and ambient calibration are also required |
| `EDDIE_INPUT_DEVICE` | Default microphone; device name or numeric index |
| `EDDIE_PIPER_MODEL` | Absolute voice model path; companion JSON required |
| `EDDIE_API_URL` | `http://127.0.0.1:8000`; use the Pi address for split deployment |

Say “Hey Eddie,” then ask your question. The detector uses 16 kHz mono audio in 80 ms frames. Recording ends after 800 ms of silence following speech, or after 10 seconds of speech. After each reply Eddie listens again for a follow-up without the wake phrase; if no speech starts within 4 seconds, the conversation ends and Eddie is dismissed. Audio stays in memory. Speech output suspends recognition and queued audio is discarded before rearming. There is no always-on cloud transcription.

Questions go to the local Ollama agent (`voice/ollama_agent.py`, #29) when it's reachable: “What's the TV doing?”, “What got blocked recently?”, “Why was device-metrics-us.amazon.com blocked?”. It only reads traffic through four tools, every number it says must come from those tools, and if Ollama is down the keyword router below answers instead. `EDDIE_AGENT=off` disables it; `EDDIE_OLLAMA_URL` and `EDDIE_AGENT_MODEL` (default `llama3.2:3b`, provisioned with `ollama pull`) configure it. Try it typed with `EDDIE_API_URL=http://127.0.0.1:8000 python voice/ollama_agent.py "What's the TV doing?"`. See [architecture](../docs/ARCHITECTURE.md#local-tool-calling-agent-29).

Keyword commands: “What's been going on?” (backend facts), “What do they know about me?” / “What do companies know about me?” (reads `GET /what-they-know?hours=168`, up to a week; the local model, when on, rewrites the backend's facts into at most five checked “Someone could guess …” lines, otherwise Eddie speaks the backend template), “turn on home mode”, “turn off home mode”, “block it” / “block device-metrics-us.amazon.com”, “allow Netflix” / “allow api.example.com” and “undo that” (below). Home mode updates backend state; actual network enforcement still depends on Will's decision engine. Lockdown and email are pending; the confirmation step lockdown and email will use is built and already guards service allows (see below). Typed questions use the same router. Do not claim these pending tools work.

## Verification before demo

- Check custom wake phrase with several speakers, background conversation and the actual speakerphone; measure false wakes and missed wakes.
- Verify leading silence, no-speech timeout, 800 ms endpoint, 10 s cap, device disconnect and queue overflow recovery.
- Confirm typed and spoken commands reach the same backend; disconnect backend and check Eddie reports unavailable facts.
- Turn off internet after provisioning; confirm wake, transcription and speech still work. The worker sets `HF_HUB_OFFLINE=1` and Whisper loads with `local_files_only`, so a missing file is an error rather than a fetch.
- Test self-trigger prevention with loud replies. No barge-in is supported yet.
- Recheck latency, sound hardware, model licenses and memory on final ARM64 hardware.

Automated command-routing (including lockdown), confirmation and provisioning tests: `python -m unittest discover -s tests -p 'test_voice*.py'`.

## Development prototype launch (2026-11-07)

Run `./scripts/run_voice.sh` from the repo root, then open http://localhost:5173. This explicitly selects `EDDIE_WAKE_ENGINE=vosk` and the provisioned Piper Lessac voice. The dashboard labels it an **offline prototype**. It recognizes the actual Hey Eddie phrase using local Vosk transcription; it is not a dedicated trained wake classifier. Pause for the listening overlay before your command. The phrase matches anywhere in Vosk's running transcript, so “…hey Eddie” after other speech wakes it, and so does reported speech like “they said hey Eddie”. Accepted variants are hey/hi/okay followed by Eddie/Eddy/Edie; bare “hey Ed” does not wake it. Standard startup still defaults to openWakeWord and reports a missing custom model rather than silently switching engines.

`GET /api/voice/status` reports readiness, engine and actionable startup errors. `POST /api/voice/start` enables capture; `POST /api/voice/stop` releases it. Both are localhost bridge endpoints.

The official Linux training notebook is prepared at `voice/training/hey_eddie.ipynb` with the target phrase changed to Hey Eddie. It has **not been executed**. It requires substantial synthetic/background datasets and a compatible training environment; its historical dependency pins need verification before execution. Do not treat this notebook as a trained model. Docker's local Linux engine is available, but no training job has been launched. Training was deferred past the hackathon (#28): the official recipe needs about 17.7 GB of downloads, its ACAV100M negative features are CC BY-NC-SA 4.0 (which would make the model non-commercial), and its pinned pipeline is partly broken (the AudioSet tar link is dead; TensorFlow 2.8.1 has no ARM64 wheels). No false-wake rate has been measured for either engine.

Checks performed: ten unit tests; synthetic Piper “Hey Eddie” recognized twice by the local Vosk prototype; synthetic “turn off home mode” did not wake it. These are software smoke tests, not microphone accuracy measurements. No synthetic audio files were persisted.


Voice recovery: after an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again, once; a second empty transcription in a row (usually background noise) ends the conversation silently. Four seconds of silence after the initial wake or during a follow-up dismisses Eddie back to the newest feed activity. Closing the overlay (×, Escape or a click outside) also ends the conversation in the voice worker (`POST /api/voice/dismiss`, localhost only), so the next request needs the wake phrase; a reply already being spoken finishes, but the overlay stays closed. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.

Conversation activity is explicit (`is_eddie_active`): wake activates it, each spoken reply starts a fresh four-second wait, and silence dismisses it. The initial wait has no retry prompt or extra four-second grace period. Speaking and transcription do not count toward the user response window.

## Block it / block a domain (#11)

“Block it”, “block that”, “can you block that tracker?” or “block device-metrics-us dot amazon dot com” adds a block rule for every device through `POST /rules` (`created_by: voice`). “It” is the last domain Eddie named in a reply this conversation, otherwise the newest flagged or allowed tracker in the last 15 minutes of traffic. The whole utterance must be the command, so “don't block it” and “block it and email Will” change nothing.

| Situation | Eddie says |
|---|---|
| Backend stored the rule and DNS loaded it (`applied: true`) | “Blocked <domain> on every device. The rule is saved and Eddie's DNS has loaded it.” |
| Stored, DNS didn't load it within 2 s (`applied: false`) | “I saved a rule to block <domain>, but Eddie's DNS hasn't loaded it yet, so it may still get through.” |
| “It” can't be resolved, or the target isn't a domain (“block Netflix”) | “Which domain should I block? Say block, then the full domain name. Nothing changed.” |
| Backend error, timeout or a reply that doesn't echo the rule | “I couldn't add that rule. Nothing changed.” |

Devices may keep using a cached answer for its TTL after a block loads. A timeout after the API already stored the rule is still reported as nothing changed; check `GET /rules`. Per-device blocks are not built; “undo” removes the rule (below).

## Allow a service or domain (#13)

“Allow Netflix”, “can you allow Prime Video?” or “allow api dot example dot com” adds global allow rules through `POST /rules` (`created_by: voice`). A global allow rule beats the allowlist, blocklists, heuristics and AI verdicts, but a device's own rule for the same domain still wins. The same whole-utterance rule as block applies: “don't allow Netflix” and “allow Netflix and block it” change nothing.

Service names map to patterns in one file, `voice/services.json`: Netflix, YouTube (“you tube”), Prime Video (“amazon prime video”) and Apple TV (“apple tv plus”). The patterns are best-effort from public knowledge of each app and have not been checked against captured traffic; a test fails if any of them would cover a domain on the shipped blocklists, so Netflix and YouTube list their hosts instead of `*.netflix.com` (which would let `logs.netflix.com` through) and `*.youtube.com`, and Prime Video leaves out `aiv-delivery.net` and `pv-cdn.net`.

| Request | What happens |
|---|---|
| A known service, or a typed wildcard (`allow *.example.com`) | Read-back first: “Allow Netflix on every device? That adds 9 allow rules for its domains. Say yes to confirm or no to cancel.” Only a yes adds the rules, one at a time. |
| One domain, spoken or typed | Added straight away, like block. |
| Anything else (“allow Hulu”, “allow it”, `*.com`) | “Which service or domain should I allow? I know Netflix, YouTube, Prime Video and Apple TV, or say allow, then the full domain name. Nothing changed.” No pattern is guessed. |

| Backend answer | Eddie says |
|---|---|
| Every rule stored and DNS loaded the last one (`applied: true`) | “Allowed <service or domain> on every device. The rules are saved and Eddie's DNS has loaded them.” |
| Stored, not loaded within 2 s | “I saved … but Eddie's DNS hasn't loaded … yet, so it may still be blocked.” |
| The first rule fails (error, timeout, or a reply that doesn't echo it) | “I couldn't add that rule. Nothing changed.” |
| A later rule of a service fails | “I only saved 2 of 9 allow rules for Netflix before the backend stopped answering, so Netflix may still be blocked. The saved rules stay.” Adding stops there; nothing is rolled back. |

Each rule waits up to 2 s for DNS, so a service with DNS down can take about 2 s per rule before Eddie answers.

## Undo the last voice rule (#15)

“Undo”, “undo that”, “never mind, undo that” or “can you undo the last one?” removes the rules from the newest voice block or allow that is still on Eddie's undo list, through `DELETE /rules/{id}`. One command is one undo: “allow Netflix” undoes all 9 of its rules at once. Saying undo again goes back one more command. The same whole-utterance rule applies, so “don't undo that” and “undo that and block it” change nothing.

The list lives in the worker's memory: up to 20 commands, kept across conversations until the worker restarts, never written to disk. Only rules the backend acknowledged to a voice `POST /rules` go on it, so undo never reaches a rule made from the dashboard or by another agent. Because SQLite can give a deleted rule's id to the next new rule, undo first reads `GET /rules` and deletes an id only if that row is still the same voice rule (pattern, action, timestamp, `created_by: voice`).

| Situation | Eddie says |
|---|---|
| Nothing on the list | “There's nothing for me to undo.” |
| Removed and DNS loaded the change (`applied: true`) | “Undone. I removed the block on <domain>, and Eddie's DNS has loaded the change.” |
| Removed, not loaded within 2 s | “I removed the …, but Eddie's DNS hasn't loaded the change yet, so it may still apply for a moment.” |
| The rule was already deleted elsewhere (404, or missing from the list) | “The block on <domain> had already been removed elsewhere, so there was nothing to undo.” It is dropped from the list; the next undo goes to the command before it. |
| Backend down or the delete isn't acknowledged | “I can't reach Eddie's backend, so I couldn't undo the …. Nothing changed.” The entry stays, so undo can be tried again. |
| A service's rules stop partway | “I only removed 2 of the 9 allow rules for Netflix before the backend stopped answering. Say undo again to remove the rest.” |

Undo has no read-back confirmation; it only puts back the previous decision. A failed block or allow leaves nothing to undo. A rule a timed-out add stored anyway isn't on the list; remove it from the dashboard.

## Lock down a device (#17)

“Lock down the TV”, “please lock down my Fire Stick” or “can you lockdown the kids' iPad?” blocks everything for one device except a keep list, after a spoken yes. The device name is matched against the labels set on the dashboard's device panel (`GET /devices`): an exact label wins, otherwise a label containing the words (“TV” finds “Living Room TV” if it's the only one). Vendor names like “Amazon device” don't count; label the device first. The keep list is every service in `voice/services.json` (Netflix, YouTube, Prime Video, Apple TV); there's no spoken way to change it yet, so “lock down the TV to Netflix” changes nothing. The allowlist's essentials (sign-in, updates, time, captive-portal checks) always survive a lockdown, so it can't brick the device.

| Request | Eddie says |
|---|---|
| One labelled device | “Lock down the TV? It'll only reach Netflix, YouTube, Prime Video and Apple TV, plus Eddie's essentials like sign-in and updates. Say yes to confirm or no to cancel.” Nothing is written yet; the confirmation table below applies (no, timeout, dismissal: nothing changes). |
| No label matches | “I don't know a device called fridge. The devices I know by name are Fire Stick and TV. Label it on the dashboard's device panel, then ask again. Nothing changed.” |
| Several labels match | “Which one: Living Room TV or Bedroom TV? Nothing changed.” |
| Already locked down | “The TV is already locked down. Nothing changed.” |
| Backend down | “I can't reach Eddie's backend, so I can't look up that device. Nothing changed.” |

On yes Eddie adds one device allow rule per keep pattern, then the device block rule `*` last (23 rules today; each waits for DNS, so expect a few seconds).

| Backend answer | Eddie says |
|---|---|
| Every rule stored and DNS loaded the last one | “The TV is locked down. The rules are saved and Eddie's DNS has loaded them. I haven't seen its traffic blocked yet, and it only applies while it uses Eddie for DNS.” |
| Stored, not loaded within 2 s | “I saved the lockdown rules for the TV, but Eddie's DNS hasn't loaded them yet, so nothing is blocked yet.” |
| The first rule fails | “I couldn't lock down the TV; the backend didn't save any rules. Nothing changed.” |
| A later rule fails, and the saved ones are deleted again | “I only saved 5 of 23 lockdown rules for the TV before the backend stopped answering, so I removed them again. Nothing is locked down.” |
| A later rule fails, and some saved ones can't be deleted | “… and couldn't remove 3 of them. Nothing is locked down, but those allow rules are still saved; say undo to remove them.” |

Because the block rule is always last, a failure never leaves the device blocked. “Undo” after a lockdown removes all of its rules at once. The dashboard's rule list shows the block as `block * · TV · by voice`.

Not yet checked on hardware: a real Fire Stick or TV under lockdown, including whether the keep patterns cover each app's sign-in and playback.

## Confirmation for risky actions

Risky actions (service and wildcard allows, lockdown; email once built) never run from the command that asks for them. `voice_agent.Confirmation` holds at most **one** pending action, `{kind, args, prompt, expires_at}`, and speaks the read-back prompt followed by “Say yes to confirm or no to cancel.”

| Next reply | Result |
|---|---|
| Exactly “yes”, “yeah”, “yep”, “yes please”, “do it”, “yes do it”, “go ahead” or “confirm” | Runs the action once. Success is spoken only if the action reports backend acknowledgment; otherwise Eddie says it couldn't confirm the change. |
| Exactly “no”, “nope”, “no thanks”, “cancel”, “cancel it”, “stop”, “don't” or “never mind” | Discards it: “Cancelled. Nothing changed.” |
| Another risky command | Replaces the pending action and says the earlier one was dropped. |
| Anything else, including missed speech | Re-prompts once; a second unclear reply discards it without running. |
| 15 seconds after the request | Expires. A late yes or no is told it expired; nothing runs. |
| Conversation dismissed, microphone stopped or errored | Discarded. |

Replies must match the whole utterance after lowercasing and removing punctuation, so “yes, but not the TV” is unclear, not a yes. Typed asks use the same router. While an action is pending the bridge emits `voice_state: confirming` with `kind`, `prompt` and `expires_in_ms` (never the action's arguments); the dashboard shows “say yes or no” until the next reply, dismissal or expiry. No pending action, reply or transcript is persisted or logged.

The registered risky actions are `allow` (#13) and `lockdown` (#17), both parsed in `route`: a typed wildcard needs the raw text's dots, and lockdown looks the device up first. `RISKY_COMMANDS` stays empty until email (#37) lands. Closing the dashboard overlay by hand does not notify the worker; the 15 second expiry still applies.


## Noisy-room voice and ready cue (#38)

`SpeechGate` uses WebRTC VAD (mode 2 by default) on four 20 ms subframes per 80 ms microphone frame. At least two positive votes plus energy above twice the measured ambient floor are required. Calibration uses the lower quintile of the last ten seconds of idle frame energies; audio is not kept for calibration. `EDDIE_VAD_MODE=0..3`, `EDDIE_NOISE_MARGIN` (default 2), and `EDDIE_SPEECH_THRESHOLD` (minimum floor, default .003) let you tune the real speakerphone. RMS alone no longer decides speech in the running worker.

A short locally generated tone plays at wake. Capture is suppressed until playback and a 160 ms acoustic tail finish, and queued audio is then discarded before the listening overlay appears. Start speaking after the tone/overlay. The command keeps 240 ms of in-memory pre-roll. Normal follow-up listening has no repeated tone.

Tests use the licensed public WebRTC speech fixture plus generated stationary noise and an impulse, and exercise suppression through cue playback, tail and queue drain. They do not establish accuracy for every room, nearby conversation, or music; rehearse with the actual microphone. No household audio or transcript fixture is created.
