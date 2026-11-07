# Eddie contributor instructions

Read README.md, docs/ARCHITECTURE.md, docs/EVENTS.md, docs/PRIVACY.md and voice/README.md before changing their components. Update docs/PRIVACY.md whenever a change affects what Eddie stores, logs, exposes or sends. Keep these documents current with meaningful architecture, setup, contract and capability changes. Distinguish implemented behavior from plans and known gaps.

- All wake detection, transcription, inference and speech must run locally after explicit model provisioning. No cloud speech fallback or implicit runtime downloads.
- Never invent traffic facts or claim a network action succeeded without backend acknowledgment. Backend mode state alone does not prove DNS enforcement.
- Keep audio in memory; do not add raw audio or transcript logging by default.
- Keep model calls off the DNS hot path. Keep voice portable between development Mac and final Linux hardware.
- Preserve unrelated work. Avoid modifying blocklists or DNS modules while building voice unless required.
- Test command routing, failure behavior and lifecycle changes. Document hardware-only checks honestly; do not claim microphone testing from unit tests.
