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
# Install (idempotent — safe to re-run after every deploy):
#   ./install-watchdog.sh
# ─────────────────────────────────────────────────────────────────────────────
set -u

# cron gives a minimal PATH (/usr/bin:/bin). `ss` lives in /usr/sbin, so
# without this the host-socket section below silently captures nothing.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

cd "$(dirname "$0")" || exit 0

URL="http://127.0.0.1:3001/healthz"
LOG="watchdog.log"
DIAG="watchdog-diag.log"
LOCK=".watchdog.lock"
STAMP=".watchdog-last-restart"
MAX_DIAG_LINES=4000
# Don't restart again within this many seconds of the last restart. The probes
# below take ~25s, so on a one-minute cron the next run can fire while the app
# is still booting — without this, a slow start turns into a restart loop.
COOLDOWN=180

# Only one watchdog at a time: a run lasts up to ~25s plus the restart, which
# overlaps the next minute's cron tick. Guarded by `command -v` on purpose —
# a bare `flock -n 9 || exit 0` on a box without flock exits 127, takes the
# `|| exit 0` branch and makes this watchdog silently do nothing forever.
# Without flock we rely on the cooldown below, which is what actually prevents
# a restart storm.
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK" || exit 0
  flock -n 9 || exit 0
fi

probe() { curl -fsS --max-time 10 "$URL" >/dev/null 2>&1; }

# Healthy on the first try: nothing to do (the common case, every minute).
probe && exit 0
# One transient failure is not enough — confirm before restarting.
sleep 15
probe && exit 0

TS="$(date -Is)"
NOW="$(date +%s)"

# Still inside the cooldown from the previous restart — the app is probably
# mid-boot. Say so and leave it alone.
if [ -f "$STAMP" ]; then
  LAST="$(tr -dc '0-9' < "$STAMP" 2>/dev/null)"
  if [ -n "${LAST:-}" ] && [ "$((NOW - LAST))" -lt "$COOLDOWN" ]; then
    echo "$TS UNHEALTHY but only $((NOW - LAST))s since last restart (cooldown ${COOLDOWN}s) — waiting" >> "$LOG"
    exit 0
  fi
fi

echo "$TS UNHEALTHY (two probes failed) — capturing diagnostics, then restarting" >> "$LOG"

# Read DB creds without sourcing .env (avoids surprises from quoting).
PGUSER="$(grep -E '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"
PGDB="$(grep -E '^POSTGRES_DB='   .env 2>/dev/null | cut -d= -f2- | tr -d '\r')"

{
  echo "════════ $TS ════════"
  echo "--- container health history ---"
  timeout 20 docker inspect --format '{{json .State.Health}}' rdc-cheque-ocr-service
  echo "--- processes inside the app container (look for stuck gunicorn workers) ---"
  timeout 20 docker compose exec -T rdc-cheque-ocr-service ps -ef
  echo "--- thread stacks of each gunicorn worker (where they are actually stuck) ---"
  timeout 30 docker compose exec -T rdc-cheque-ocr-service sh -c \
    'for p in $(pgrep -f "gunicorn: worker"); do echo "== pid $p =="; cat /proc/$p/task/*/stack 2>/dev/null; cat /proc/$p/wchan 2>/dev/null; echo; done'
  echo "--- host socket states (many CLOSE_WAIT = sockets not being closed) ---"
  ss -tan 2>/dev/null | awk 'NR>1 {print $1}' | sort | uniq -c
  echo "--- connections to the app port (3001) by state ---"
  ss -tan 2>/dev/null | awk '$4 ~ /:3001$/ {print $1}' | sort | uniq -c
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

echo "$NOW" > "$STAMP"
docker compose restart rdc-cheque-ocr-service >> "$LOG" 2>&1
echo "$(date -Is) restart issued (exit $?)" >> "$LOG"

# Keep the diagnostic log from growing without bound.
if [ -f "$DIAG" ] && [ "$(wc -l < "$DIAG")" -gt "$MAX_DIAG_LINES" ]; then
  tail -n "$MAX_DIAG_LINES" "$DIAG" > "$DIAG.tmp" && mv "$DIAG.tmp" "$DIAG"
fi
