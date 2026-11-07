#!/bin/sh
# Explicit laptop prototype; a dedicated model remains the production default.
set -eu
cd "$(dirname "$0")/.."
export EDDIE_WAKE_ENGINE="${EDDIE_WAKE_ENGINE:-vosk}"
export EDDIE_PIPER_MODEL="${EDDIE_PIPER_MODEL:-$PWD/voice/models/piper/en_US-lessac-medium.onnx}"
exec .venv-frontend/bin/python -B frontend/serve.py "$@"
