#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Watchdog for the RDC PDC Cheque Tracker.
#
# Why this exists: `restart: unless-stopped` only fires when a process EXITS.
# When gunicorn wedges (every worker thread stuck on something that never
# returns) the container stays alive and Docker does nothing, so the site is
# down until someone intervenes. This probes /healthz and, if the app is not
# answering, captures evidence and restarts JUST the app container. Postgres
# and its data are untouched.
#
# Install once:
#   chmod +x ~/projects/rdc-pdc-app/watchdog.sh
#   crontab -e
#   * * * * * /home/developer/projects/rdc-pdc-app/watchdog.sh
# ─────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$0")" || exit 0

URL="http://127.0.0.1:3001/healthz"
LOG="watchdog.log"
DIAG="watchdog-diag.log"
MAX_DIAG_LINES=4000

probe() { curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; }

# Healthy on the first try: nothing to do (the common case, every minute).
probe && exit 0
# One transient failure is not enough — confirm before restarting.
sleep 15
probe && exit 0

TS="$(date -Is)"
echo "$TS UNHEALTHY (two probes failed) — capturing diagnostics, then restarting" >> "$LOG"

# Read DB creds without sourcing .env (avoids surprises from quoting).
PGUSER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"
PGDB="$(grep -E '^POSTGRES_DB='   .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"

{
  echo "════════ $TS ════════"
  echo "--- processes inside the app container (look for stuck gunicorn workers) ---"
  timeout 20 docker compose exec -T rdc-cheque-ocr-service ps -ef
  echo "--- host socket states (many CLOSE_WAIT = sockets not being closed) ---"
  ss -tan 2>/dev/null | awk 'NR>1 {print $1}' | sort | uniq -c
  echo "--- postgres sessions (longest running first) ---"
  timeout 20 docker compose exec -T rdc-postgres-db psql -p 3002 \
    -U "${PGUSER:-pdc_user}" -d "${PGDB:-rdc_cheque_ocr}" -c \
    "select pid, state, wait_event_type, wait_event,
            now() - query_start as running_for, left(query, 80) as query
       from pg_stat_activity
      where datname = current_database()
      order by running_for desc nulls last limit 20;"
  echo "--- last 80 app log lines ---"
  timeout 20 docker compose logs --tail 80 rdc-cheque-ocr-service
} >> "$DIAG" 2>&1

docker compose restart rdc-cheque-ocr-service >> "$LOG" 2>&1
echo "$(date -Is) restart issued (exit $?)" >> "$LOG"

# Keep the diagnostic log from growing without bound.
if [ -f "$DIAG" ] && [ "$(wc -l < "$DIAG")" -gt "$MAX_DIAG_LINES" ]; then
  tail -n "$MAX_DIAG_LINES" "$DIAG" > "$DIAG.tmp" && mv "$DIAG.tmp" "$DIAG"
fi
