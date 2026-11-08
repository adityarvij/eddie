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

On the Pi (#18) the voice loop runs there too, with the speakerphone plugged into the Pi. Create the voice environment as `.venv-frontend` in the repo (`python3 -m venv .venv-frontend && .venv-frontend/bin/pip install -r voice/requirements.txt`, after `sudo apt install libportaudio2`), run `.venv-frontend/bin/python scripts/provision_voice.py`, then rerun `sudo ./scripts/install_pi.sh`. The `eddie-dashboard` service then runs `scripts/run_voice.sh` in the `audio` group, and Ollama runs on the Pi. Start the microphone from the dashboard on the Pi's own screen (`http://localhost:5173`); a paired iPad can type asks but can't turn the mic on. ARM64 wheels, the speakerphone, latency and memory alongside Ollama haven't been checked on the Pi yet.

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
| `EDDIE_API_TOKEN` | Bearer token for the traffic API's writes (rules, modes); needed when the API isn't on this machine's loopback, such as in Docker or on the Pi |
| `EDDIE_TIMERS_FILE` | `voice/timers.json`; pending timers and reminders. `off` keeps them in memory only, so a restart loses them |
| `EDDIE_HOUSE_GUIDE` | `voice/house_guide.json`; the concierge house guide, read on each question. Missing means an empty guide |
| `EDDIE_ALARM_SOUND` | Unset plays the generated chime; or a local 16-bit PCM `.wav` (first 10 s played) |

Say “Hey Eddie,” then ask your question. The detector uses 16 kHz mono audio in 80 ms frames. Recording ends after 800 ms of silence following speech, or after 10 seconds of speech. After each reply Eddie listens again for a follow-up without the wake phrase; if no speech starts within 4 seconds, the conversation ends and Eddie is dismissed. Audio stays in memory. Speech output suspends recognition and queued audio is discarded before rearming. There is no always-on cloud transcription.

Questions go to the local Ollama agent (`voice/ollama_agent.py`, #29) when it's reachable: “What's the TV doing?”, “What got blocked recently?”, “Why was device-metrics-us.amazon.com blocked?”. It reads traffic through four tools, every number it says must come from those tools, and if Ollama is down the keyword router below answers instead. It can also propose a change (see [plain-English rules](#plain-english-rules-108)); nothing runs without a spoken yes. `EDDIE_AGENT=off` disables it; `EDDIE_OLLAMA_URL` and `EDDIE_AGENT_MODEL` (default `llama3.2:3b`, provisioned with `ollama pull`) configure it. Try it typed with `EDDIE_API_URL=http://127.0.0.1:8000 python voice/ollama_agent.py "What's the TV doing?"`. See [architecture](../docs/ARCHITECTURE.md#local-tool-calling-agent-29).

Keyword commands: “What's been going on?” (backend facts), “What do they know about me?” / “What do companies know about me?” (honors ranges such as “the last 3 days,” otherwise uses all retained history; longer requests are capped with disclosure, and Eddie says there is not enough data instead of guessing; the local model, when on, rewrites the backend's facts into at most five checked “Someone could guess …” lines, otherwise Eddie speaks the backend template), “When could someone tell the house was empty?” / “When was nobody home in the last 3 days?” (reads `GET /burglars-eye`, a week unless the question names a range; Eddie speaks the backend's text, which says when the answer is from less data than asked for or there's too little to tell), “turn on home mode”, “turn off home mode”, “I'm leaving for the weekend” / “I'm back” (away mode, below), “block it” / “block device-metrics-us.amazon.com”, “allow Netflix” / “allow api.example.com” and “undo that” (below). Home mode updates backend state; actual network enforcement still depends on Will's decision engine. Lockdown and email are pending; the confirmation step lockdown and email will use is built and already guards service allows (see below). Typed questions use the same router. Do not claim these pending tools work.

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

`GET /api/voice/status` reports readiness, engine and actionable startup errors. `POST /api/voice/start` enables capture; `POST /api/voice/stop` releases it. Both are localhost bridge endpoints. The dashboard only calls start: to turn the mic off, say “stop recording” (also “stop listening”, “turn off the mic”, “mic off”). The router matches those whole phrases before anything else, with no yes needed, and the local model has a `stop_microphone` tool for other wordings. Eddie says “Okay, I'm turning the microphone off,” then the stream closes (“The microphone is already off.” if it was).

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

## Ask before blocking (#79)

The AI only flags likely trackers; Eddie asks before anything is blocked. When you wake Eddie and the dashboard has an open question he hasn't read yet, he reads it first: “I've got one question for you. 3 new likely trackers on the TV: a.example, b.example and c.example. Block them? Say yes to confirm or no to cancel.” Each question is read once.

| Reply | Eddie says |
|---|---|
| Yes, DNS loaded the rules | “Blocked a.example, b.example and c.example on every device. Eddie's DNS has loaded the change.” |
| Yes, DNS didn't confirm (stopped or slow) | “I saved a block for …, but Eddie's DNS hasn't loaded it, so it isn't confirmed blocked yet.” |
| Yes, but it was answered on the dashboard first | “That question was already answered. Nothing changed.” |
| No, silence, or 15 s pass | Nothing is written; the question stays on the dashboard. |

“Undo” removes the rules a yes created, like any voice block. The question lookup waits at most 1 s; if the backend is down Eddie skips it and listens as usual. A spoken yes on the real microphone still needs a hardware check.

## Privacy suggestions (#112)

“How can I improve my privacy?”, “privacy tips”, “any suggestions?”, “how do I make my Fire Stick more private?” or “what settings should I change?” reads the backend's guide for each recognised device (`GET /suggestions`): “For Fire Stick, on the TV open Settings, Preferences, Privacy Settings and turn off Device Usage Data, Collect App Usage Data and Interest-based Ads. … I haven't changed any of these myself.” Guidance only: nothing is confirmed or written. With nothing recognised Eddie names the guides he has (Fire TV and Ring); if the backend is down he says he can't read the device list. The steps and source links are under **suggestions** on the dashboard.

## Sensitivity modes (#110)

“Protect anything health related”, “protect the kids”, “protect anything to do with money”, “turn on news protection” turn a sensitivity mode on; “stop protecting health” or “turn off kids protection” turn it off. Category words: health (medical, pharmacy), kids (children), money (finance, banking, shopping) and beliefs (news, religion, politics). The whole utterance must be the command, so “don't protect health” or “protect the kids' tablet” change nothing. Like home mode it runs without a yes, since it only blocks trackers. Eddie posts `POST /sensitivity` and reads back only the answer: with `applied: true`, that the mode is on and DNS loaded it, which sites open the 5-minute window (“like BetterHelp, CVS or GoodRx”), how many trackers and brokers it blocks there, and that the sites keep working; with `applied: false`, that it's saved but nothing is blocked yet; on a backend error or a mismatched echo, that nothing changed. Tested with a stubbed backend (`tests/test_voice_sensitivity.py`); not yet spoken on hardware.

## Lock down a device (#17)

“Lock down the TV”, “please lock down my Fire Stick” or “can you lockdown the kids' iPad?” blocks everything for one device except a keep list, after a spoken yes. The device name is matched against the names set in the dashboard's device list (`GET /devices`): an exact label wins, otherwise a label containing the words (“TV” finds “Living Room TV” if it's the only one). Vendor names like “Amazon device” don't count, and nor does `---`, which is how the list shows a device with no name; name the device first. The keep list is every streaming service in `voice/services.json` (Netflix, YouTube, Prime Video, Apple TV), or the kid-safe list for a device marked as a child's (below); there's no spoken way to change it yet, so “lock down the TV to Netflix” changes nothing. The allowlist's essentials (sign-in, updates, time, captive-portal checks) always survive a lockdown, so it can't brick the device.

| Request | Eddie says |
|---|---|
| One labelled device | “Lock down the TV? It'll only reach Netflix, YouTube, Prime Video and Apple TV, plus Eddie's essentials like sign-in and updates. Say yes to confirm or no to cancel.” Nothing is written yet; the confirmation table below applies (no, timeout, dismissal: nothing changes). |
| No label matches | “I don't know a device called fridge. The devices I know by name are Fire Stick and TV; 1 more has no name yet and shows as ---. Name it in the dashboard's device list, then ask again. Nothing changed.” |
| A device marked as a child's | “Lock down the kids' tablet? It's marked as a child's, so it'll only reach YouTube Kids and PBS Kids, plus Eddie's essentials like sign-in and updates. Say yes to confirm or no to cancel.” |
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

## Kid mode (#119)

Mark a device as a child's in the dashboard's device list (the `devices` link, bottom right: each row's `adult` / `child` button). Then “turn on kid mode”, “kid mode on” or “lock down the kids' devices” locks down every child's device not already locked down, and “turn off kid mode” or “unlock the kids' devices” unlocks them. Both read back the devices first and wait for a spoken yes. The dashboard's `kid mode` switch does the same after its own confirm dialog.

The kid-safe keep list is `voice/kid_safe.json` (seeded with YouTube Kids and PBS Kids, both defined in `voice/services.json` with `"kids": true`); edit it to change what kid mode allows, then restart the worker. DNS can't tell the YouTube Kids app from YouTube: YouTube Kids keeps `youtubei.googleapis.com` and the video CDNs, which the YouTube app also uses, but not `youtube.com`.

| Request | Eddie says |
|---|---|
| On | “Turn on kid mode? Kids' Tablet and an unnamed Amazon device will only reach YouTube Kids and PBS Kids, plus Eddie's essentials like sign-in and updates. Say yes to confirm or no to cancel.” |
| Yes, every rule stored and loaded | “Kid mode is on. Kids' Tablet and an unnamed Amazon device are locked down to YouTube Kids and PBS Kids, plus Eddie's essentials; Eddie's DNS has loaded the rules. I haven't seen their traffic blocked yet, and it only applies while they use Eddie for DNS.” |
| Off | “Turn off kid mode? Kids' Tablet will get full internet through Eddie again. Say yes to confirm or no to cancel.” On yes, each device's `*` block goes first, then its kid-safe allow rules; its other rules stay. |
| No child's device | “No device is marked as a child's yet. Open devices on the dashboard and set one to child, then ask again. Nothing changed.” |
| Already on (or off) | “Kid mode is already on for Kids' Tablet. Nothing changed.” |

A device that fails or isn't loaded by DNS gets its own sentence (the lockdown table's wording), and kid mode isn't called on. Kid mode is on when every child's device has a `*` block rule; it has no stored state of its own. A device marked as a child's while kid mode is on isn't covered until kid mode is turned on again, and marking a locked device as an adult's doesn't unlock it (the dashboard asks first). Tests: `tests/test_voice_kid_mode.py`. Not yet checked on hardware: a real tablet in kid mode.

The registered risky actions are `allow` (#13) and `lockdown` (#17), both parsed in `route`: a typed wildcard needs the raw text's dots, and lockdown looks the device up first. `block` and `home_mode` are registered for the model's write tools (#108) only; spoken “block x.com” and “turn on home mode” still run directly. `RISKY_COMMANDS` stays empty until email (#37) lands. Closing the dashboard overlay by hand does not notify the worker; the 15 second expiry still applies.

## New device (#98)

“Allow the new device” (also “approve” or “let in”) and “deny the new device” (also “block” or “reject”) act on the newest device `GET /devices` lists as `pending`. Eddie reads it back by name and first-seen time (“Allow the new device, kindle, first seen at 2:14 AM?”) and says how many others are waiting; only a yes posts `/devices/<mac>/approve` or `/deny`. The reply comes from the backend's stored status, and says Eddie's DNS has loaded it only when the API answered `applied: true`. With no pending device, or the backend down, nothing changes. Tested in `tests/test_voice_new_device.py`; not checked with a real device joining.

## Timers and reminders (#122)

`voice/timers.py` parses these locally; they never reach the model or the traffic API, and need no confirmation.

| Say | Eddie |
|---|---|
| “Set a timer for 10 minutes”, “set a 10 minute timer”, “timer for an hour and a half” | “Timer set for 10 minutes.” Up to 24 hours. |
| “Remind me in 20 minutes to check the oven”, “remind me to call my mom at 6 PM”, “remind me tomorrow at 7 to run”, “set a reminder for 5 to leave” | “Okay, I'll remind you at 6 PM to call your mom.” Up to 7 days ahead. |
| “Remind me to call mom” (no time) | Asks when; nothing is set. |
| “What timers do I have?”, “how much time is left?”, “list my reminders” | Each one, with time left or when it's due. |
| “Cancel the timer”, “cancel the 10 minute timer”, “cancel the reminder to call mom”, “cancel all timers” | Cancels it. With several and no way to tell which, Eddie lists them and cancels nothing. |

A bare hour (“at 6”) means the next 6 o'clock, AM or PM; “tomorrow at 7” is the morning and “tomorrow at 3” the afternoon. Eddie always says the time back with AM or PM, so cancel and ask again if he picked the wrong one. A time already past today (“at 2 PM today” at 3:30) sets nothing. At most 20 at once.

When one is due, the bridge (`frontend/serve.py`) plays the alarm, then says it: “Your timer for 10 minutes is done.” or “Reminder to call your mom.” The dashboard shows “Timer done” or “Reminder”. This works with the microphone off; it waits for any reply Eddie is speaking. The alarm is a generated chime (three rising beeps, three times, about 3.4 s), so there's nothing to provision; `EDDIE_ALARM_SOUND` swaps in a local WAV. It rings once and doesn't repeat until stopped.

They persist in `EDDIE_TIMERS_FILE` (owner-only JSON, rewritten on each change). One that came due while the bridge was off rings as soon as it restarts: “Sorry, I'm late. Your timer for 10 minutes finished at 3:30 PM.” Each item is removed before it rings, so a crash mid-alarm never rings it twice. An unreadable file is reported at startup and replaced on the next change. Tested in `tests/test_voice_timers.py`; the chime hasn't been checked on the Pi's speaker.

## House guide (#165)

Guests can ask Eddie about the house: “What's the Wi-Fi password?”, “what time is checkout?”, “where are the towels?”, “can I park on the street?”, “how do I start the dishwasher?”. Eddie answers with the owner's own words from a local file, quoted as written. `voice/house_guide.py` does the lookup; the guide never reaches the local model or the traffic API, and needs no network.

**Set it up.** Copy `voice/house_guide.example.json` to `voice/house_guide.json` (gitignored) and write your own entries. `EDDIE_HOUSE_GUIDE` points at another file. Edits take effect on the next question; no restart.

```json
{"entries": [
  {"topic": "Wi-Fi", "keywords": ["wifi", "internet", "network"], "answer": "The network is ...", "audience": "guest"},
  {"topic": "alarm code", "keywords": ["alarm"], "answer": "...", "audience": "owner"}
]}
```

| Field | |
|---|---|
| `topic` | Required. Also a keyword, and the name Eddie uses when he asks which one you mean. |
| `answer` | Required. Spoken word for word. Keep it to a sentence or two. |
| `keywords` | Optional. Other words or phrases a question might use. Matched as whole words, with an optional plural s; case, hyphens and apostrophes are ignored (“Wi-Fi” is “wifi”). |
| `audience` | `guest` or `owner`. **Defaults to `owner`**: an entry is spoken to guests only when marked `guest`. |

Any other field, a missing topic or answer, or an unknown audience makes the file invalid; Eddie then says “I can't read the house guide right now. Please ask the host.”

**How it answers.** An utterance is a house question when it is phrased as a question (what, where, when, how, is, can I, tell me…) and names a guide keyword or a common house topic (Wi-Fi, checkout, towels, parking, laundry, thermostat, trash, hot tub…), or when it is just a topic's name (“the dishwasher”, the reply to “which one?”). The entry whose keywords cover the most words wins, so “washing machine” beats “dishwasher” in a question naming both. A tie asks “I have notes on dishwasher and washing machine. Which one?” No matching entry is “I don't know that one. Please ask the host.” It runs after timers and before home mode, block, allow and the model; “what does the TV know about me”, empty-house and privacy-tips questions skip it.

**Owner-only entries.** Concierge roles (#163) aren't built yet, so Eddie can't tell a guest from the owner and always answers as a guest (`voice_agent.house_guide_role = 'guest'`): owner entries are never spoken. A guest asking about one hears the same don't-know as for a topic that isn't there, and “which one?” never names an owner topic. Owner entries are for the owner reading the file until #163 sets the role.

Tested in `tests/test_voice_house_guide.py` (schema, lookup, ambiguity, missing answers, owner-only entries, the starter example, and that nothing is printed or logged). Not tried with real guests or on the Pi's microphone. **Gap:** a question the guide doesn't recognise as about the house (“where's the nearest coffee shop?”) still goes to the local model, which can answer from its own knowledge; guest routing is #166.

## Text a contact

“Text Will, I'm running late”, “message Sam: back by 6” or “send a text to Will saying on my way” drafts a text to someone in Eddie's contacts. Eddie reads it back word for word, the way Whisper heard it (“Text Will: "I'm running late." Send it? Say yes to confirm or no to cancel.”), and sends it only on a yes within **30 seconds** (longer than other confirmations, so there's time to hear the read-back). No, an unclear reply twice, the timeout or dismissing the conversation sends nothing. The text is never rewritten by the local model, and the model has no texting tool.

**Where it goes.** Eddie sends through the Messages app on the Mac that runs the voice loop, signed in to your Apple ID: by iMessage (through Apple's servers, end-to-end encrypted) or, for contacts marked `SMS`, through your iPhone's Text Message Forwarding and then your carrier. The sent text shows up in Messages on your other Apple devices. There's no cloud SMS API and no texting on Linux yet (see [known gaps](#texting-gaps)).

**Setup (macOS only):**

1. Copy `voice/contacts.example.json` to `voice/contacts.json` (gitignored) and list the people Eddie may text: `{"Will": "+15551234567", "Sam": {"address": "+15557654321", "service": "SMS"}}`. An address is an E.164 number or an email; `service` is `iMessage` (default) or `SMS`. `EDDIE_CONTACTS` points at another file. Eddie never texts a number you say aloud, only a name on this list (a misheard digit would reach a stranger).
2. Sign in to Messages on the Mac. For SMS, turn on Text Message Forwarding for the Mac on the iPhone (Settings → Apps → Messages).
3. Start Eddie with `EDDIE_TEXTING=messages`. It's off by default, and refused on Linux.
4. The first send asks to let the app running Eddie (Terminal, iTerm, …) control Messages. Allow it.
5. To hear “Sent”, give that app **Full Disk Access** (System Settings → Privacy & Security). Eddie needs it to read `~/Library/Messages/chat.db`, where Messages records whether a text went out. Eddie reads only the newest outgoing message's sent and error flags for that contact, never message text.

| After the yes | Eddie says |
|---|---|
| Messages marks it sent with no error (checked for up to 10 s) | “Sent to Will.” |
| Messages records an error, or refuses the send | “Messages couldn't send that to Will. Check Messages before trying again.” |
| Handed to Messages, not marked sent within 10 s | “I handed that to Messages, but it hasn't confirmed it went out to Will. …” |
| Handed to Messages, but `chat.db` can't be read (no Full Disk Access) | “… I can't read its status on this Mac, so I can't confirm it went out to Will. …” |

Unknown names (“I can only text Will or Sam”), an empty message, a message over 160 characters, missing or invalid `contacts.json` and texting being off are all spoken refusals; nothing is pending. Tested in `tests/test_voice_text.py` with a faked `osascript` and a stand-in `chat.db`; the AppleScript was compiled against macOS 26's Messages dictionary but **no real text has been sent by the tests**. Check a send to your own number on the Mac before relying on it.

### Texting gaps

- Linux and the Pi have no sender. A local Android SMS gateway would fit; an iPhone can't be driven from the LAN.
- `is_sent` means Apple's server or the carrier took the text, not that it was delivered.
- An iMessage to a number that isn't on iMessage fails; mark that contact `SMS`.
- The read-back shows on the dashboard too (see the confirmation section).

## Confirmation for risky actions

Risky actions (service and wildcard allows, lockdown, texts, and every write the local model proposes; email once built) never run from the command that asks for them. `voice_agent.Confirmation` holds at most **one** pending action, `{kind, args, prompt, expires_at}`, and speaks the read-back prompt followed by “Say yes to confirm or no to cancel.”

| Next reply | Result |
|---|---|
| Exactly “yes”, “yeah”, “yep”, “yes please”, “do it”, “yes do it”, “go ahead” or “confirm” | Runs the action once. Success is spoken only if the action reports backend acknowledgment; otherwise Eddie says it couldn't confirm the change. |
| Exactly “no”, “nope”, “no thanks”, “cancel”, “cancel it”, “stop”, “don't” or “never mind” | Discards it: “Cancelled. Nothing changed.” |
| Another risky command | Replaces the pending action and says the earlier one was dropped. |
| Anything else, including missed speech | Re-prompts once; a second unclear reply discards it without running. |
| 15 seconds after the request (30 for a text) | Expires. A late yes or no is told it expired; nothing runs. |
| Conversation dismissed, microphone stopped or errored | Discarded. |

Replies must match the whole utterance after lowercasing and removing punctuation, so “yes, but not the TV” is unclear, not a yes. Typed asks use the same router. While an action is pending the bridge emits `voice_state: confirming` with `kind`, `prompt` and `expires_in_ms` (never the action's arguments); the dashboard shows “say yes or no” until the next reply, dismissal or expiry. A text's `prompt` contains the message, so it is on screen like the transcript. No pending action, reply or transcript is persisted or logged.

The registered risky actions are `allow` (#13), `lockdown` (#17), `kid_mode` (#119), `approve` / `deny` for new devices (#98) and `text`, all parsed in `route`: a typed wildcard needs the raw text's dots, lockdown and new devices look the device up first, and a text keeps the raw transcript word for word. `block` and `home_mode` are registered for the model's write tools (#108) only; spoken “block x.com” and “turn on home mode” still run directly. `RISKY_COMMANDS` stays empty until email (#37) lands. Closing the dashboard overlay by hand does not notify the worker; the 15 second expiry still applies.


## Noisy-room voice and ready cue (#38)

`SpeechGate` uses WebRTC VAD (mode 2 by default) on four 20 ms subframes per 80 ms microphone frame. At least two positive votes plus energy above twice the measured ambient floor are required. Calibration uses the lower quintile of the last ten seconds of idle frame energies; audio is not kept for calibration. `EDDIE_VAD_MODE=0..3`, `EDDIE_NOISE_MARGIN` (default 2), and `EDDIE_SPEECH_THRESHOLD` (minimum floor, default .003) let you tune the real speakerphone. RMS alone no longer decides speech in the running worker.

A short locally generated tone plays at wake. Capture is suppressed until playback and a 160 ms acoustic tail finish, and queued audio is then discarded before the listening overlay appears. Start speaking after the tone/overlay. The command keeps 240 ms of in-memory pre-roll. Normal follow-up listening has no repeated tone.

Tests use the licensed public WebRTC speech fixture plus generated stationary noise and an impulse, and exercise suppression through cue playback, tail and queue drain. They do not establish accuracy for every room, nearby conversation, or music; rehearse with the actual microphone. No household audio or transcript fixture is created.
