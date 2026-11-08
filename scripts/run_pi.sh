#!/usr/bin/env bash
# Start Eddie on the Raspberry Pi (#18), no Docker: DNS on port 53, the traffic API on :8000,
# the classifier, and the dashboard on :5173 with the voice loop when .venv-frontend is set up.
# Nothing here needs Ollama: the classifier and voice use small local models.
#   ./scripts/run_pi.sh
# Ctrl-C stops everything. To start on boot instead, use scripts/install_pi.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "$(uname -s)" != Linux ]]; then
  echo "run_pi.sh is for the Pi (Linux). On the Mac use ./scripts/run_all.sh." >&2
  exit 1
fi
[[ -x .venv/bin/python ]] || uv sync --locked
py="$PWD/.venv/bin/python"

# Everything here reaches the API on loopback; the token is for writers elsewhere, such as the Mac's voice loop.
# Set it yourself to share it with the Mac; otherwise it's random per run and never printed (#96).
export EDDIE_API_TOKEN="${EDDIE_API_TOKEN:-$("$py" -c 'import secrets; print(secrets.token_urlsafe(24))')}"
export EDDIE_DASHBOARD_HOST="${EDDIE_DASHBOARD_HOST:-0.0.0.0}"
port="${EDDIE_DNS_PORT:-53}"

# Port 53 needs CAP_NET_BIND_SERVICE. Ask for sudo now, not from a background job, and drop
# straight back to this user with only that capability, so eddie.db stays ours rather than root's.
dns=("$py" -m eddie.dns_server)
if (( port < 1024 && EUID != 0 )); then
  sudo -v
  dns=(sudo -E setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups
       --inh-caps=+net_bind_service --ambient-caps=+net_bind_service -- "${dns[@]}")
fi

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

"$py" -m uvicorn eddie.api:app --host 0.0.0.0 --port 8000 & pids+=($!)
for _ in $(seq 50); do
  "$py" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/mode', timeout=1)" 2>/dev/null && break
  sleep 0.2
done
"${dns[@]}" & pids+=($!)
"$py" -m eddie.classifier & pids+=($!)
if [[ -x .venv-frontend/bin/python ]]; then
  ./scripts/run_voice.sh & pids+=($!)
else
  echo "No .venv-frontend: dashboard only, no microphone (see voice/README.md)."
  "$py" -B frontend/serve.py & pids+=($!)
fi

ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "DNS: ${ip:-<Pi IP>}:$port   API: http://${ip:-<Pi IP>}:8000   Dashboard: http://${ip:-<Pi IP>}:5173"
# One process dying stops the rest, so a half-running Eddie isn't mistaken for a working one.
wait -n
