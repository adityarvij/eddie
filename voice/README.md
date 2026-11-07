# Offline voice setup

Run from the Eddie repository root with Python 3.11. The laptop is a development host; the audio worker is separate from DNS and can move to the final Linux box.

## Provision once while online

1. Install `voice/requirements.txt` in a dedicated virtual environment. On Linux install PortAudio if sounddevice needs it. Verify platform wheels before moving to Pi ARM64.
2. Train a custom **hey eddie** model using the official openWakeWord training notebook. Export ONNX to `voice/models/hey_eddie.onnx`. An arbitrary filename does not change what an existing model recognizes. Do not rename Hey Jarvis and present it as Hey Eddie.
3. Provision openWakeWord's shared embedding and mel-spectrogram ONNX assets in its package model directory using its setup utility (`openwakeword.utils.download_models()`) while online. The worker does not call a downloader.
4. Provision Whisper locally, for example with `huggingface_hub.snapshot_download('Systran/faster-whisper-base.en', local_dir='voice/models/whisper-base.en')`. This is a setup download, not a runtime speech API.
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

Supported commands: “What's been going on?” (backend facts), “What do they know about me?” / “What do companies know about me?” (reads `GET /what-they-know`; facts computed by the backend, no model phrasing yet), “turn on home mode”, “turn off home mode”. Home mode updates backend state; actual network enforcement still depends on Will's decision engine. Domain rules, lockdown, email and general Ollama conversation are pending; the confirmation step they will use is built (see below). Typed questions use the same router. Do not claim these pending tools work.

## Verification before demo

- Check custom wake phrase with several speakers, background conversation and the actual speakerphone; measure false wakes and missed wakes.
- Verify leading silence, no-speech timeout, 800 ms endpoint, 10 s cap, device disconnect and queue overflow recovery.
- Confirm typed and spoken commands reach the same backend; disconnect backend and check Eddie reports unavailable facts.
- Turn off internet after provisioning; confirm wake, transcription and speech still work.
- Test self-trigger prevention with loud replies. No barge-in is supported yet.
- Recheck latency, sound hardware, model licenses and memory on final ARM64 hardware.

Automated command-routing and confirmation tests: `python -m unittest discover -s tests -p 'test_voice*.py'`.

## Development prototype launch (2026-11-07)

Run `./scripts/run_voice.sh` from the repo root, then open http://localhost:5173. This explicitly selects `EDDIE_WAKE_ENGINE=vosk` and the provisioned Piper Lessac voice. The dashboard labels it an **offline prototype**. It recognizes the actual Hey Eddie phrase using local Vosk transcription; it is not a dedicated trained wake classifier. Pause for the listening overlay before your command. Standard startup still defaults to openWakeWord and reports a missing custom model rather than silently switching engines.

`GET /api/voice/status` reports readiness, engine and actionable startup errors. `POST /api/voice/start` enables capture; `POST /api/voice/stop` releases it. Both are localhost bridge endpoints.

The official Linux training notebook is prepared at `voice/training/hey_eddie.ipynb` with the target phrase changed to Hey Eddie. It has **not been executed**. It requires substantial synthetic/background datasets and a compatible training environment; its historical dependency pins need verification before execution. Do not treat this notebook as a trained model. Docker's local Linux engine is available, but no training job has been launched.

Checks performed: ten unit tests; synthetic Piper “Hey Eddie” recognized twice by the local Vosk prototype; synthetic “turn off home mode” did not wake it. These are software smoke tests, not microphone accuracy measurements. No synthetic audio files were persisted.


Voice recovery: after an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again. Four seconds of silence after the initial wake or during a follow-up dismisses Eddie back to the newest feed activity. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.

Conversation activity is explicit (`is_eddie_active`): wake activates it, each spoken reply starts a fresh four-second wait, and silence dismisses it. The initial wait has no retry prompt or extra four-second grace period. Speaking and transcription do not count toward the user response window.

## Confirmation for risky actions

Risky actions (lockdown and email, once built) never run from the command that asks for them. `voice_agent.Confirmation` holds at most **one** pending action, `{kind, args, prompt, expires_at}`, and speaks the read-back prompt followed by “Say yes to confirm or no to cancel.”

| Next reply | Result |
|---|---|
| Exactly “yes”, “yeah”, “yep”, “yes please”, “do it”, “yes do it”, “go ahead” or “confirm” | Runs the action once. Success is spoken only if the action reports backend acknowledgment; otherwise Eddie says it couldn't confirm the change. |
| Exactly “no”, “nope”, “no thanks”, “cancel”, “cancel it”, “stop”, “don't” or “never mind” | Discards it: “Cancelled. Nothing changed.” |
| Another risky command | Replaces the pending action and says the earlier one was dropped. |
| Anything else, including missed speech | Re-prompts once; a second unclear reply discards it without running. |
| 15 seconds after the request | Expires. A late yes or no is told it expired; nothing runs. |
| Conversation dismissed, microphone stopped or errored | Discarded. |

Replies must match the whole utterance after lowercasing and removing punctuation, so “yes, but not the TV” is unclear, not a yes. Typed asks use the same router. While an action is pending the bridge emits `voice_state: confirming` with `kind`, `prompt` and `expires_in_ms` (never the action's arguments); the dashboard shows “say yes or no” until the next reply, dismissal or expiry. No pending action, reply or transcript is persisted or logged.

No risky action is registered yet: `RISKY_ACTIONS` and `RISKY_COMMANDS` are empty until lockdown (#31) and email (#37) land, so today this flow is only reachable from unit tests. Closing the dashboard overlay by hand does not notify the worker; the 15 second expiry still applies.


## Noisy-room voice and ready cue (#38)

`SpeechGate` uses WebRTC VAD (mode 2 by default) on four 20 ms subframes per 80 ms microphone frame. At least two positive votes plus energy above twice the measured ambient floor are required. Calibration uses the lower quintile of the last ten seconds of idle frame energies; audio is not kept for calibration. `EDDIE_VAD_MODE=0..3`, `EDDIE_NOISE_MARGIN` (default 2), and `EDDIE_SPEECH_THRESHOLD` (minimum floor, default .003) let you tune the real speakerphone. RMS alone no longer decides speech in the running worker.

A short locally generated tone plays at wake. Capture is suppressed until playback and a 160 ms acoustic tail finish, and queued audio is then discarded before the listening overlay appears. Start speaking after the tone/overlay. The command keeps 240 ms of in-memory pre-roll. Normal follow-up listening has no repeated tone.

Tests use the licensed public WebRTC speech fixture plus generated stationary noise and an impulse, and exercise suppression through cue playback, tail and queue drain. They do not establish accuracy for every room, nearby conversation, or music; rehearse with the actual microphone. No household audio or transcript fixture is created.
