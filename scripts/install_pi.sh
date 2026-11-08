#!/usr/bin/env bash
# Install Eddie's systemd services on the Pi so it starts on boot (#18).
#   uv sync --locked                     # once, as your own user (plus the voice setup in voice/README.md)
#   sudo ./scripts/install_pi.sh         # Ollama on the Pi; --ollama http://<Mac IP>:11434 uses the Mac's
#   sudo systemctl start eddie.target    # or reboot
# Rerun after moving the repo, setting up voice or changing the Ollama address; the tokens in the env file are kept.
# It also enables eddie-update.timer, which keeps the Pi on origin/main (scripts/update_pi.sh).
# --dest DIR writes the units and env file into DIR and leaves systemd alone (used by the tests).
set -euo pipefail
cd "$(dirname "$0")/.."
repo="$PWD"
py="$repo/.venv/bin/python"
ollama="" dest=""
while (( $# )); do
  case "$1" in
    --ollama) ollama="${2:?--ollama needs a URL}"; shift 2 ;;
    --dest) dest="${2:?--dest needs a directory}"; shift 2 ;;
    *) echo "usage: sudo $0 [--ollama http://<Mac IP>:11434]" >&2; exit 2 ;;
  esac
done

if [[ -n "$dest" ]]; then
  units="$dest" env_file="$dest/eddie.env" user="$(id -un)"
else
  [[ "$(uname -s)" == Linux ]] || { echo "install_pi.sh is for the Pi (Linux)." >&2; exit 1; }
  if (( EUID != 0 )) || [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == root ]]; then
    echo "Run it with sudo from the account that owns the repo: sudo $0" >&2
    exit 1
  fi
  units=/etc/systemd/system env_file=/etc/eddie/eddie.env user="$SUDO_USER"
fi
# The path goes into ExecStart unquoted and through sed, so keep it plain.
if [[ "$repo" =~ [[:space:]%#\\] ]]; then
  echo "Move the repo to a path without spaces, %, # or backslashes: $repo" >&2
  exit 1
fi
[[ -x "$py" ]] || { echo "Run 'uv sync --locked' in $repo as $user first." >&2; exit 1; }
if [[ -n "$ollama" ]] && ! "$py" -c 'import sys; from eddie.classifier import check_local; check_local(sys.argv[1])' "$ollama" 2>/dev/null; then
  echo "--ollama must be http:// on localhost or a private IP, such as http://172.20.10.2:11434" >&2
  exit 1
fi

# The env file holds the API and pairing tokens, so only root (systemd) can read it.
umask 077
mkdir -p "$(dirname "$env_file")"
if [[ ! -f "$env_file" ]]; then
  {
    echo "# Eddie service settings (#18), read by systemd. Holds tokens: keep it 0600."
    echo "EDDIE_API_TOKEN=$("$py" -c 'import secrets; print(secrets.token_urlsafe(24))')"
    echo "EDDIE_DASHBOARD_TOKEN=$("$py" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  } > "$env_file"
fi
if [[ -n "$ollama" ]]; then
  grep -v '^EDDIE_OLLAMA_URL=' "$env_file" > "$env_file.tmp" || true
  echo "EDDIE_OLLAMA_URL=$ollama" >> "$env_file.tmp"
  mv "$env_file.tmp" "$env_file"
fi
chmod 600 "$env_file"

# With the voice environment the dashboard also runs the microphone loop; without it, dashboard and typed asks only.
dashboard="$repo/.venv/bin/python -B frontend/serve.py"
[[ -x "$repo/.venv-frontend/bin/python" ]] && dashboard="$repo/scripts/run_voice.sh"

for template in deploy/systemd/*; do
  out="$units/$(basename "$template")"
  sed -e "s#@DASHBOARD@#$dashboard#g" -e "s#@REPO@#$repo#g" -e "s#@USER@#$user#g" -e "s#@ENV@#$env_file#g" \
    "$template" > "$out"
  chmod 644 "$out"
done
[[ -n "$dest" ]] && exit 0

systemctl daemon-reload
systemctl enable eddie.target eddie-api.service eddie-dns.service eddie-classifier.service eddie-dashboard.service
# Follows origin/main every 5 minutes; sudo touch /etc/eddie/hold pauses it.
systemctl enable --now eddie-update.timer
ip="$(hostname -I | awk '{print $1}')"
token="$(sed -n 's/^EDDIE_DASHBOARD_TOKEN=//p' "$env_file")"
echo "Installed; Eddie starts on boot. Start it now: sudo systemctl start eddie.target"
echo "Logs: journalctl -u 'eddie-*' -f"
echo "Updates from main every 5 minutes. Before a demo: sudo touch /etc/eddie/hold"
systemctl is-enabled --quiet ollama.service 2>/dev/null || grep -q '^EDDIE_OLLAMA_URL=' "$env_file" ||
  echo "No ollama service on this Pi: install Ollama and pull the model, or the classifier and typed asks wait for it."
[[ "$dashboard" == */run_voice.sh ]] || echo "No .venv-frontend: dashboard only, no microphone (see voice/README.md, then rerun this)."
echo "Pair the iPad once by opening http://${ip:-<Pi IP>}:5173/#pair=$token"
