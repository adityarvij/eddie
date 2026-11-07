# Eddie architecture: voice implementation status

The backend API and DNS enforcement are independent of the voice worker. Development currently uses the Mac microphone and a localhost dashboard; production should run the same offline worker on the final box after ARM64 performance checks.

Audio → openWakeWord custom ONNX → bounded in-memory capture → faster-whisper local model → deterministic tool router → local REST API → Piper → audio output.

The dashboard bridge publishes `voice_status`, `wake`, `voice_state`, `transcript`, `agent_reply`, `speaking`, and `speech_end` events through its existing SSE connection. It accepts start, stop and typed ask requests. Speech and typed responses share a serialized lock. Recognition discards input during replies and resets detector history before rearming.

Implemented tools are backend summary reads and home-mode writes. Home mode is backend state, not evidence of DNS enforcement. Missing tools explicitly report that no change occurred. The model does not fabricate counts. Ollama tool calling is a future layer over the same tool boundary; use structured validated arguments, bounded iterations and confirmation state for lockdown/email.

## Remaining work

- Train and evaluate the actual Hey Eddie model; record provenance, license, threshold and false-wake metrics.
- Provision and hash all local assets; add a repeatable offline installer and hardware compatibility checks.
- Integrate Will's rule endpoints only after their request/response contract exists; add read-back, expiry and undo semantics.
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
