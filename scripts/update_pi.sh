#!/usr/bin/env bash
# Keep the Pi on the latest main (#18). eddie-update.timer runs this as root every 5 minutes;
# by hand: sudo ./scripts/update_pi.sh <repo owner>.
# Hold updates before a demo, since a restart drops DNS answers for a few seconds:
#   sudo touch /etc/eddie/hold      # resume: sudo rm /etc/eddie/hold
# Only a new main restarts eddie.target. A failed dependency install rolls the code back and restarts nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
user="${1:?usage: sudo $0 <repo owner>}"
hold="${EDDIE_UPDATE_HOLD:-/etc/eddie/hold}"
if [[ -e "$hold" ]]; then
  echo "Updates held ($hold)."
  exit 0
fi

# git and uv run as the repo owner, so the checkout and the environments keep one owner.
as_user() { runuser -u "$user" -- "$@"; }
home="$(getent passwd "$user" | cut -d: -f6)"
uv="$home/.local/bin/uv"
[[ -x "$uv" ]] || uv="$(command -v uv)"

as_user git fetch --quiet origin main
old="$(as_user git rev-parse HEAD)"
new="$(as_user git rev-parse origin/main)"
[[ "$old" == "$new" ]] && exit 0
changed="$(as_user git diff --name-only "$old" "$new")"

rollback() {
  echo "Update to ${new:0:7} failed; staying on ${old:0:7}." >&2
  as_user git checkout --quiet --force -B main "$old"
  as_user "$uv" sync --locked --quiet || true
  exit 1
}
# Untracked files (eddie.db, voice/models, the environments) are left alone.
as_user git checkout --quiet --force -B main origin/main
as_user "$uv" sync --locked --quiet || rollback
if [[ -x .venv-frontend/bin/python ]]; then
  if grep -qx 'voice/requirements.txt' <<<"$changed"; then
    as_user env VIRTUAL_ENV=.venv-frontend "$uv" pip install --quiet -r voice/requirements.txt || rollback
  fi
  if grep -qx 'voice/assets.json' <<<"$changed"; then
    # The full run ends with a hardware check, which can fail on its own; --verify decides.
    as_user .venv-frontend/bin/python -B scripts/provision_voice.py >/dev/null 2>&1 || true
    as_user .venv-frontend/bin/python -B scripts/provision_voice.py --verify || rollback
  fi
fi
# New or changed units, or a changed installer, need the units rendered again.
if grep -qE '^(deploy/systemd/|scripts/install_pi\.sh$)' <<<"$changed"; then
  SUDO_USER="$user" ./scripts/install_pi.sh >/dev/null
fi
systemctl restart eddie.target
echo "Updated ${old:0:7} -> ${new:0:7}."
