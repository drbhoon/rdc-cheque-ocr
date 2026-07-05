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
| `prod` | Production — Docker | Company Azure server → **pdc.rdcc.ai** (LAN 192.168.100.7:3001) |

- Local folder may sit on `prod`; check `git branch` before editing.
- Railway uses Nixpacks (`railway.json`, gunicorn start command). Docker files
  (Dockerfile / docker-compose.yml / .env.example) exist only on `prod`.
- Prod deploy: server `developer@RDC-AI-UBUNTU:~/projects/rdc-pdc-app`,
  `git pull && docker compose up -d --build`.
- Daily reminder cron: cron-job.org hits `GET /cron/reminders?key=<CRON_SECRET>` daily
  ~9:00 IST (do NOT use Railway cron — it restarts the service).

## Prod ports (prod branch)
Container ports EQUAL host ports (8000/5432 clash with other apps on the server):
app listens on **3001** (Dockerfile EXPOSE + gunicorn bind), Postgres on **3002**
(compose `command: -p 3002`, healthcheck `-p 3002`, ports `3002:3002`), and
DATABASE_URL uses host `rdc-postgres-db:3002`.

## Stack
Python 3.11 + Flask (single `app.py`) · Gunicorn (2 workers × 4 threads, 120s timeout) ·
PostgreSQL (psycopg2-binary) · Claude `claude-sonnet-4-6` vision for OCR · openpyxl ·
Jinja templates in `templates/` (index=scan, dashboard, report, staff, users, reminders,
login, password) · company logo at `static/rdc-logo.jpg` on every page header.

## Employee master & roles (drives EVERYTHING)
`employees` table, uploaded as Excel on the Staff Master page — columns
**EMP_CODE, EMP_NAME, ROLE, EMAIL, LOCATION, PLANT** (one row per employee, key EMP_CODE).
- **ROLE → app authority at login** (session role; users-table role is only the
  fallback, e.g. seeded admin): SALES→scan/save · ACCOUNTS→scan+lifecycle ·
  BH/RM→VIEWER (read-only) · HO/ADMIN→HO_ADMIN. Mapping in `EMP_ROLE_MAP`.
- **ROLE+LOCATION → routing**: each location has an ACCOUNTS + BH pair; a cheque is
  auto-assigned to the pair covering the sales person's location. ACCOUNTS/BH rows
  covering several locations list them comma-separated in LOCATION.
- **Uploader gate**: nobody can save cheques unless their login email is in the
  employee master (emp_code stamped on every cheque as `cheques.emp_code`).
  Optional ERP `cust_code` entered at scan or set later from dashboard (audited).
- **Pairing audit** (`pairing_warnings()`): runs on every Staff page load + upload;
  sticky amber box lists locations with missing/duplicate ACCOUNTS or BH.
- Legacy `staff_master` table still exists in DB but is unused.

## Logins
`users` table. First HO Admin seeded from `ADMIN_EMAIL`/`ADMIN_PASSWORD` while table
empty. **Bulk logins**: Users page → "Create logins from Staff Master" gives every
employee-master person without a login the password `Welcome@123`
(`DEFAULT_PASSWORD`) + `must_change_password` — forced to set their own at first
login (`/password`, enforced by a `before_request` guard). HO rows are excluded from
bulk creation — admins are added manually with strong passwords. Admin password
resets also force a change at next login.

## Cheque lifecycle
PENDING → DEPOSITED → CLEARED, or BOUNCED → (RE)DEPOSITED / LEGAL / RTGS_SETTLED → CLOSED.
Plus SECURITY (undated collateral cheques: no tracking/reminders, HO-Admin-deletable).
- "Expired" = 90-day bank validity lapsed (cheque_date + 90 < today), NOT past due date.
- Cheque Date & Amount FROZEN after save; only HO_ADMIN `/cheque/<id>/override` with a
  mandatory reason (audited in `change_log`).
- Every lifecycle action records `done_by`; creation records `created_by` + `emp_code`.
- Reminders: daily digests from 7 days before due date to Accounts incharge + BH (own
  cheques) + HO mailbox, de-duplicated. CLEARED/RTGS/CLOSED/DEPOSITED/SECURITY excluded.

## DB tables (auto-created/migrated in `init_db()` on startup)
`cheques` (OCR + lifecycle + staff snapshot + emp_code/cust_code), `employees`
(employee master), `staff_master` (legacy, unused), `cheque_events`, `users`
(+must_change_password), `change_log`. Duplicate guard: unique
(account_number, cheque_number) partial index → 409 on re-save.

## OCR prompt hard-won rules (in `PROMPT`, app.py — don't regress these)
- Bank name = top-left logo; ignore bottom rubber stamps (collecting bank).
- Cheque number = LEFTMOST MICR block only; MICR E-13B font: scrutinise first digit (1↔9↔7).
- Cheque date = ONLY the labelled `D D M M Y Y Y Y` boxes; ignore stationery/print dates;
  photo may be rotated — read by labels not position.
- Account number = printed "A/c No." field digit-for-digit; never pad; null if unsure.
- Amounts: words vs figures cross-checked in UI (match badge).

## Conventions
- All displayed dates DD/MM/YYYY (`dmy` filter; scan uses DD/MM/YYYY text inputs, ISO
  internally). Excel export keeps its original widely-tested column set — do NOT add
  columns without an explicit ask.
- Frontend: vanilla JS in templates; pass row data via `data-*` attributes, NEVER inline
  `|tojson` in onclick (breaks on quotes/decimals).
- Every DB write route: rollback on error.
- `User Test Data -ignore/` is local-only (gitignored) — never commit it.

## Env vars
`ANTHROPIC_API_KEY`, `DATABASE_URL`, `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
`SMTP_HOST`(smtp.gmail.com), `SMTP_PORT`(587), `SMTP_USER`, `SMTP_APP_PASSWORD`,
`SMTP_FROM`(noreply@rdc.in), `HO_REMINDER_EMAIL`(creditcontrol.ho@rdc.in), `CRON_SECRET`.
Docker/prod additionally: `POSTGRES_USER/PASSWORD/DB` (see `.env.example`; `.env` gitignored).

## Live users
Prod seeded HO Admin: aniket.sawant@rdc.in (from server .env). Staff manual:
`D:\RDC Drive\AI\Cowork\PDC Cheque Tracker - Staff User Manual.docx`.
