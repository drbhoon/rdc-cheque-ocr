#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Installs watchdog.sh on the current user's crontab, every minute.
#
# Idempotent: re-running replaces the existing entry rather than adding a
# second one, so this is safe to run after every deploy. Run it as the user
# that owns the Docker containers (developer), NOT with sudo — a root crontab
# would run `docker compose` in the wrong home directory.
#
#   cd ~/projects/rdc-pdc-app && ./install-watchdog.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd -P)"
LINE="* * * * * $DIR/watchdog.sh"

if [ ! -f "$DIR/watchdog.sh" ]; then
  echo "ERROR: watchdog.sh not found in $DIR" >&2
  exit 1
fi
chmod +x "$DIR/watchdog.sh"

# Build the new crontab in a temp file. Doing this via a pipeline is a trap:
# `crontab -l | grep -v` exits non-zero when there is no crontab yet, and under
# `set -e` that can install an EMPTY crontab.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
crontab -l 2>/dev/null | grep -vF 'watchdog.sh' > "$TMP" || true
echo "$LINE" >> "$TMP"
crontab "$TMP"

echo "Installed crontab entry:"
crontab -l | grep -F 'watchdog.sh' | sed 's/^/  /'

# A crontab on a box where cron is not running is exactly the silent failure
# this watchdog exists to prevent — so check.
if command -v systemctl >/dev/null 2>&1; then
  for svc in cron crond; do
    if systemctl list-unit-files "$svc.service" >/dev/null 2>&1 \
       && systemctl cat "$svc.service" >/dev/null 2>&1; then
      if systemctl is-active --quiet "$svc"; then
        echo "cron service ($svc): active"
      else
        echo "WARNING: the $svc service is NOT active — the watchdog will never fire." >&2
        echo "         Start it with: sudo systemctl enable --now $svc" >&2
      fi
      break
    fi
  done
fi

if ! command -v flock >/dev/null 2>&1; then
  echo "NOTE: flock is not installed — the watchdog falls back to its cooldown" >&2
  echo "      guard only. Optional: sudo apt install -y util-linux" >&2
fi

# Prove the probe the watchdog relies on actually works right now.
if curl -fsS --max-time 10 http://127.0.0.1:3001/healthz >/dev/null 2>&1; then
  echo "health probe: OK (app is answering on 3001)"
else
  echo "NOTE: the app is not answering /healthz on 3001 right now." >&2
  echo "      The watchdog will restart it within ~2 minutes." >&2
fi
