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
| `EDDIE_SPEECH_THRESHOLD` | `0.015` normalized RMS; tune on the actual microphone |
| `EDDIE_INPUT_DEVICE` | Default microphone; device name or numeric index |
| `EDDIE_PIPER_MODEL` | Absolute voice model path; companion JSON required |
| `EDDIE_API_URL` | `http://127.0.0.1:8000`; use the Pi address for split deployment |

Say “Hey Eddie,” then ask your question. The detector uses 16 kHz mono audio in 80 ms frames. Recording ends after 800 ms of silence following speech, or after 10 seconds of speech. After each reply Eddie listens again for a follow-up without the wake phrase; if no speech starts within 4 seconds, the conversation ends and Eddie is dismissed. Audio stays in memory. Speech output suspends recognition and queued audio is discarded before rearming. There is no always-on cloud transcription.

Supported commands: “What's been going on?” (backend facts), “turn on home mode”, “turn off home mode”. Home mode updates backend state; actual network enforcement still depends on Will's decision engine. Domain rules, lockdown, email, follow-up confirmation and general Ollama conversation are pending. Typed questions use the same router. Do not claim these pending tools work.

## Verification before demo

- Check custom wake phrase with several speakers, background conversation and the actual speakerphone; measure false wakes and missed wakes.
- Verify leading silence, no-speech timeout, 800 ms endpoint, 10 s cap, device disconnect and queue overflow recovery.
- Confirm typed and spoken commands reach the same backend; disconnect backend and check Eddie reports unavailable facts.
- Turn off internet after provisioning; confirm wake, transcription and speech still work.
- Test self-trigger prevention with loud replies. No barge-in is supported yet.
- Recheck latency, sound hardware, model licenses and memory on final ARM64 hardware.

Automated command-routing tests: `python -m unittest discover -s tests -p 'test_voice*.py'`.

## Development prototype launch (2026-11-07)

Run `./scripts/run_voice.sh` from the repo root, then open http://localhost:5173. This explicitly selects `EDDIE_WAKE_ENGINE=vosk` and the provisioned Piper Lessac voice. The dashboard labels it an **offline prototype**. It recognizes the actual Hey Eddie phrase using local Vosk transcription; it is not a dedicated trained wake classifier. Pause for the listening overlay before your command. Standard startup still defaults to openWakeWord and reports a missing custom model rather than silently switching engines.

`GET /api/voice/status` reports readiness, engine and actionable startup errors. `POST /api/voice/start` enables capture; `POST /api/voice/stop` releases it. Both are localhost bridge endpoints.

The official Linux training notebook is prepared at `voice/training/hey_eddie.ipynb` with the target phrase changed to Hey Eddie. It has **not been executed**. It requires substantial synthetic/background datasets and a compatible training environment; its historical dependency pins need verification before execution. Do not treat this notebook as a trained model. Docker's local Linux engine is available, but no training job has been launched.

Checks performed: ten unit tests; synthetic Piper “Hey Eddie” recognized twice by the local Vosk prototype; synthetic “turn off home mode” did not wake it. These are software smoke tests, not microphone accuracy measurements. No synthetic audio files were persisted.


Voice recovery: after an initial wake with no command, or an empty/failed transcription, Eddie says “Didn’t catch that. Try again.” and listens again. Four seconds of silence during a follow-up dismisses Eddie back to the newest feed activity. Speech inference errors recover without disabling wake detection; microphone errors and bridge disconnects close the overlay. Listening/waiting uses a yellow ambient glow, speaking uses blue. Automated voice checks pass; live microphone/noisy-room accuracy still needs rehearsal.
