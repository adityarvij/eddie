#!/bin/sh
# Explicit laptop prototype; a dedicated model remains the production default.
set -eu
cd "$(dirname "$0")/.."
export EDDIE_WAKE_ENGINE="${EDDIE_WAKE_ENGINE:-vosk}"
export EDDIE_PIPER_MODEL="${EDDIE_PIPER_MODEL:-$PWD/voice/models/piper/en_US-lessac-medium.onnx}"
# Fail loudly on missing or altered models before opening the microphone (#39).
.venv-frontend/bin/python -B scripts/provision_voice.py --verify
exec .venv-frontend/bin/python -B frontend/serve.py "$@"
