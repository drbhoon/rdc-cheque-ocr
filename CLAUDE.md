# RDC PDC Cheque Tracker (rdc-cheque-ocr)

Post-Dated Cheque (PDC) management app for RDC Concrete (India) Ltd. Scans cheque
images/PDFs with Claude vision, tracks the deposit lifecycle, and emails daily reminders.

**This project covers ONLY this app.** The AI ledger-reconciliation app is a separate
project (`D:\RDC Drive\AI\Cowork\rdc-ledger-reconciliation`, repo
drbhoon/rdc-ledger-reconciliation) — never edit it from this chat, and vice-versa.

## Deployments & branches
| Branch | Purpose | Target |
|---|---|---|
| `main` | Development — auto-deploys | Railway → **https://pdc.bhoon.org** |
| `prod` | Production — Docker | Self-hosted Linux server (Azure) via docker compose |

- Local folder may sit on `prod`; check `git branch` before editing.
- Railway uses Nixpacks (`railway.json`, gunicorn start command). Docker files
  (Dockerfile / docker-compose.yml / .env.example) exist only on `prod`.
- Daily reminder cron: cron-job.org hits `GET /cron/reminders?key=<CRON_SECRET>` daily
  ~9:00 IST (do NOT use Railway cron — it restarts the service).

## Stack
Python 3.11 + Flask (single `app.py`, ~1,500 lines) · Gunicorn (2 workers × 4 threads,
120s timeout) · PostgreSQL (psycopg2-binary) · Claude `claude-sonnet-4-6` vision for OCR ·
openpyxl (Excel export + staff upload) · Jinja templates in `templates/`
(index=scan, dashboard, report, staff, users, reminders, login).

## Auth & roles
Session login (werkzeug hashes). First HO Admin seeded from `ADMIN_EMAIL`/`ADMIN_PASSWORD`
only while `users` table is empty; after that manage users in-app (👤 Users).
| Role | Can |
|---|---|
| HO_ADMIN | everything: users, staff master, reminders, override locked fields, delete security cheques |
| ACCOUNTS | scan/save + full cheque lifecycle |
| SALES | scan/save only |
| VIEWER | read-only dashboard/report/export |
Post-login landing is role-based (uploaders → scan, viewer → dashboard). Forbidden pages
redirect to the user's landing; JSON/POST return 403.

## Cheque lifecycle
PENDING → DEPOSITED → CLEARED, or BOUNCED → (RE)DEPOSITED / LEGAL / RTGS_SETTLED → CLOSED.
Plus SECURITY (undated collateral cheques: no tracking/reminders, HO-Admin-deletable).
- "Expired" = 90-day bank validity lapsed (cheque_date + 90 < today), NOT past due date.
  Pending and Expired are mutually exclusive buckets.
- Cheque Date & Amount are FROZEN after save; only HO_ADMIN `/cheque/<id>/override` with a
  mandatory reason (audited in `change_log`, shown in History → Edit Log).
- Every lifecycle action records `done_by`; cheque creation records `created_by`
  (History shows "CREATED · by <user>").
- Reminders: daily digests from 7 days before deposit-due date, to each Accounts incharge +
  each Business Head (their own cheques) + HO mailbox (all), de-duplicated by email.
  CLEARED / RTGS_SETTLED / CLOSED / DEPOSITED / SECURITY excluded; LEGAL persists until Close.

## DB tables (auto-created/migrated in `init_db()` on startup)
`cheques` (OCR fields + lifecycle + staff snapshot + created_by + cheque_location),
`staff_master` (Excel-uploaded, key SALES_NAME), `cheque_events` (timeline, remarks,
done_by), `users`, `change_log` (audit). Duplicate guard: unique (account_number,
cheque_number) partial index → 409 on re-save.

## OCR prompt hard-won rules (in `PROMPT`, app.py — don't regress these)
- Bank name = top-left logo; ignore bottom rubber stamps (collecting bank).
- Cheque number = LEFTMOST MICR block only; MICR E-13B font: scrutinise first digit (1↔9↔7).
- Cheque date = ONLY the labelled `D D M M Y Y Y Y` boxes; ignore stationery/print dates
  (e.g. "CAV/2024/UF 09/12/24"); photo may be rotated — read by labels not position.
- Account number = printed "A/c No." field digit-for-digit; never pad; null if unsure.
- Amounts: words vs figures cross-checked in UI (match badge).

## Conventions
- All displayed dates DD/MM/YYYY (`dmy` Jinja filter; scan screen uses DD/MM/YYYY text
  inputs, ISO internally). Excel date cells formatted DD/MM/YYYY.
- Frontend: vanilla JS in templates; pass row data via `data-*` attributes, NEVER inline
  `|tojson` in onclick (breaks on quotes/decimals).
- On save the scan screen fully resets; camera capture supported on mobile.
- Every DB write route: rollback on error; gunicorn multi-worker so one hang ≠ outage.

## Env vars
`ANTHROPIC_API_KEY`, `DATABASE_URL`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
`SMTP_HOST`(smtp.gmail.com), `SMTP_PORT`(587), `SMTP_USER`, `SMTP_APP_PASSWORD`,
`SMTP_FROM`(noreply@rdc.in), `HO_REMINDER_EMAIL`(creditcontrol.ho@rdc.in), `CRON_SECRET`.
Docker/prod additionally: `POSTGRES_USER/PASSWORD/DB` (see `.env.example`; `.env` gitignored).

## Prod ports (prod branch)
Container ports EQUAL host ports (8000/5432 clash with other apps on the server):
app listens on **3001** (Dockerfile EXPOSE + gunicorn bind), Postgres on **3002**
(compose `command: -p 3002`, healthcheck `-p 3002`, ports `3002:3002`), and
DATABASE_URL uses host `rdc-postgres-db:3002`. Backend team redeploys with
`docker compose up -d --build` after updating their `.env` DATABASE_URL port.

## Testing users (test phase)
Login ksbhoon@rdc.in exists as HO_ADMIN. Staff manual:
`D:\RDC Drive\AI\Cowork\PDC Cheque Tracker - Staff User Manual.docx`.
