#!/usr/bin/env bash
# Start Eddie for development: API + fake traffic in Docker, dashboard and voice bridge on the host.
#   ./scripts/run_all.sh            with fake traffic
#   ./scripts/run_all.sh --no-fake  API only (real DNS server posting to :8000)
# Ctrl-C stops the dashboard; the containers keep running until `docker compose down`.
set -euo pipefail
cd "$(dirname "$0")/.."

# Docker Desktop's CLI and credential helper live here when it hasn't been added to PATH.
command -v docker >/dev/null || PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
profile=(--profile fake)
[[ "${1:-}" == "--no-fake" ]] && profile=()

docker compose ${profile[@]+"${profile[@]}"} up -d --build --wait

echo "API: http://localhost:8000   Dashboard: http://localhost:5173"
# Uses the Vosk wake prototype and Piper voice until the custom Hey Eddie model is trained.
if [[ ${#profile[@]} -gt 0 ]]; then
  uv run python -m eddie.feed_classifier &
  classifier_pid=$!
  trap 'kill "$classifier_pid" 2>/dev/null || true' EXIT
fi
./scripts/run_voice.sh
