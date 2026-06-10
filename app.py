import os
import re
import json
import base64
import io
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify, render_template, send_file
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp", "pdf"}
STAFF_EXTENSIONS = {"xlsx", "xls"}

_client = None


# ── Anthropic client ────────────────────────────────────────────────────────

def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ── Database helpers ─────────────────────────────────────────────────────────

def db_url():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_db():
    import psycopg2
    return psycopg2.connect(db_url())


def init_db():
    """Create / migrate all tables. Safe to run on every startup."""
    conn = get_db()
    try:
        cur = conn.cursor()

        # Base cheques table (Part I) — kept for backward compatibility
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cheques (
                id             SERIAL PRIMARY KEY,
                bank_name      TEXT,
                account_number TEXT,
                cheque_date    TEXT,
                payee          TEXT,
                amount_words   TEXT,
                amount_numbers TEXT,
                issuer_name    TEXT,
                scanned_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Part II additions — add columns if they don't exist yet
        cheque_cols = [
            ("cheque_number",     "TEXT"),
            ("cheque_date_iso",   "DATE"),
            ("deposit_due_date",  "DATE"),
            ("amount_value",      "NUMERIC"),
            ("status",            "TEXT DEFAULT 'PENDING'"),
            ("deposited_date",    "DATE"),
            ("deposit_bank",      "TEXT"),
            ("deposit_reference", "TEXT"),
            ("cleared_date",      "DATE"),
            ("bounce_date",       "DATE"),
            ("bounce_reason",     "TEXT"),
            ("sales_name",        "TEXT"),
            ("sales_email",       "TEXT"),
            ("location",          "TEXT"),
            ("plant",             "TEXT"),
            ("accounts_email",    "TEXT"),
            ("bh_name",           "TEXT"),
            ("bh_email",          "TEXT"),
            ("cheque_location",   "TEXT DEFAULT 'Customer'"),
            ("created_at",        "TIMESTAMPTZ DEFAULT NOW()"),
            ("updated_at",        "TIMESTAMPTZ"),
        ]
        for name, ddl in cheque_cols:
            cur.execute(f"ALTER TABLE cheques ADD COLUMN IF NOT EXISTS {name} {ddl}")

        # Duplicate protection — only enforced when both values are present
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_cheque_acct_no
            ON cheques (account_number, cheque_number)
            WHERE account_number IS NOT NULL AND cheque_number IS NOT NULL
        """)

        # Staff master (uploaded via Excel)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS staff_master (
                sales_name     TEXT PRIMARY KEY,
                sales_email    TEXT,
                location       TEXT,
                plant          TEXT,
                accounts_email TEXT,
                bh_name        TEXT,
                bh_email       TEXT,
                updated_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Event / audit trail (deposit lifecycle, re-deposits)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cheque_events (
                id          SERIAL PRIMARY KEY,
                cheque_id   INTEGER REFERENCES cheques(id) ON DELETE CASCADE,
                action      TEXT,
                action_date DATE,
                bank        TEXT,
                reference   TEXT,
                reason      TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        conn.commit()
        cur.close()
    finally:
        conn.close()


with app.app_context():
    try:
        init_db()
    except Exception as _e:
        print(f"[DB] init skipped: {_e}")


# ── Claude prompt ─────────────────────────────────────────────────────────────

PROMPT = """You are scanning an Indian bank cheque. Extract these fields and return them as a single valid JSON object.

Fields:
1. bank_name       — The DRAWER's bank. In India this is the bank name/logo printed at the TOP-LEFT of the cheque. IMPORTANT: ignore any rubber stamps near the bottom — those belong to the collecting/receipt bank, NOT the drawer's bank.
2. account_number  — Read ONLY the digits printed in the "A/c No." field. Read them exactly, digit-for-digit. Do NOT pad, do NOT add leading or trailing zeros, do NOT read the MICR band at the very bottom. If you cannot read it confidently, return null.
3. cheque_number   — The cheque number: in the MICR code line at the very bottom of the cheque, take ONLY the LEFTMOST block of digits (usually 6 digits, printed between ⑈ symbols). IGNORE all blocks to the right of it (those are the MICR/city-bank-branch code, account code and transaction code). Return null if not readable.
4. date            — The date exactly as it appears (e.g. "14/7/22"), raw.
5. cheque_date_iso — The same date normalised to ISO format YYYY-MM-DD. Prefer the boxed DD MM YYYY date (usually top-right) as it is most reliable; otherwise use the handwritten date. Return null if no date can be determined.
6. payee           — Name in the "Pay" field.
7. amount_words    — Amount written in words on the "Rupees" line (e.g. "Five Lakhs Only").
8. amount_numbers  — Amount in the numeric box (e.g. "5,00,000").
9. issuer_name     — Account holder / drawer name printed at the bottom of the cheque.

Rules:
- Return ONLY a raw JSON object, no markdown fences, no explanation.
- Use null for any field you cannot read confidently. Never guess.

Example:
{"bank_name":"ICICI Bank","account_number":"015601500005","cheque_number":"000501","date":"08/06/2026","cheque_date_iso":"2026-06-08","payee":"RDC Concrete","amount_words":"Five Lakhs Only","amount_numbers":"5,00,000","issuer_name":"BRIG K S BHOON"}"""


def build_source(img_bytes, media_type):
    b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
    if media_type == "application/pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}}


def read_cheque(img_bytes, media_type="image/jpeg"):
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=700,
        messages=[
            {
                "role": "user",
                "content": [
                    build_source(img_bytes, media_type),
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw.strip())


# ── Utilities ─────────────────────────────────────────────────────────────────

def parse_amount(s):
    """'5,00,000' or '₹ 5,00,000.50' → float, else None."""
    if not s:
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(s)))
    except ValueError:
        return None


def parse_iso_date(s):
    """Return a date for a 'YYYY-MM-DD' string, else None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def file_ext(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


# ── Routes: scan UI ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/predict", methods=["POST"])
def predict():
    if "image" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    ext = file_ext(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Unsupported file type"}), 400

    img_bytes = file.read()
    if ext == "pdf":
        media_type = "application/pdf"
    elif ext in {"jpg", "jpeg"}:
        media_type = "image/jpeg"
    else:
        media_type = f"image/{ext}"

    try:
        data = read_cheque(img_bytes, media_type)
        return jsonify({"cheque": data})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Could not parse cheque data: {e}"}), 500
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── Routes: staff master ─────────────────────────────────────────────────────

STAFF_FIELDS = ["sales_name", "sales_email", "location", "plant",
                "accounts_email", "bh_name", "bh_email"]


@app.route("/staff")
def staff_list():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(STAFF_FIELDS)} FROM staff_master ORDER BY sales_name")
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()
    return jsonify([dict(zip(STAFF_FIELDS, r)) for r in rows])


@app.route("/staff/upload", methods=["GET", "POST"])
def staff_upload():
    if request.method == "GET":
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM staff_master")
            count = cur.fetchone()[0]
            cur.close()
        finally:
            conn.close()
        return render_template("staff.html", count=count)

    # POST — parse uploaded xlsx
    if "file" not in request.files or request.files["file"].filename == "":
        return jsonify({"error": "No file selected"}), 400
    file = request.files["file"]
    if file_ext(file.filename) not in STAFF_EXTENSIONS:
        return jsonify({"error": "Please upload an .xlsx file"}), 400

    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file.read()), data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        return jsonify({"error": f"Could not read Excel: {e}"}), 400

    if not rows or len(rows) < 2:
        return jsonify({"error": "Sheet is empty or has no data rows"}), 400

    # Map header → column index (case-insensitive, by expected names)
    header = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    try:
        idx = {f: header.index(f) for f in STAFF_FIELDS}
    except ValueError:
        return jsonify({
            "error": "Header row must contain columns: " + ", ".join(STAFF_FIELDS)
        }), 400

    conn = get_db()
    saved = 0
    try:
        cur = conn.cursor()
        for r in rows[1:]:
            name = r[idx["sales_name"]] if idx["sales_name"] < len(r) else None
            if not name or str(name).strip() == "":
                continue
            vals = [str(r[idx[f]]).strip() if idx[f] < len(r) and r[idx[f]] is not None else None
                    for f in STAFF_FIELDS]
            cur.execute(
                """
                INSERT INTO staff_master
                    (sales_name, sales_email, location, plant, accounts_email, bh_name, bh_email, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s, NOW())
                ON CONFLICT (sales_name) DO UPDATE SET
                    sales_email=EXCLUDED.sales_email, location=EXCLUDED.location,
                    plant=EXCLUDED.plant, accounts_email=EXCLUDED.accounts_email,
                    bh_name=EXCLUDED.bh_name, bh_email=EXCLUDED.bh_email, updated_at=NOW()
                """,
                vals,
            )
            saved += 1
        conn.commit()
        cur.close()
        return jsonify({"success": True, "saved": saved})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── Routes: save cheque ──────────────────────────────────────────────────────

@app.route("/accept", methods=["POST"])
def accept():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data received"}), 400

    account_number = (data.get("account_number") or "").strip() or None
    cheque_number  = (data.get("cheque_number") or "").strip() or None

    conn = get_db()
    try:
        cur = conn.cursor()

        # Explicit duplicate check for a friendly message
        if account_number and cheque_number:
            cur.execute(
                "SELECT id FROM cheques WHERE account_number=%s AND cheque_number=%s",
                (account_number, cheque_number),
            )
            existing = cur.fetchone()
            if existing:
                cur.close()
                return jsonify({
                    "error": "duplicate",
                    "message": f"Cheque #{cheque_number} on account {account_number} "
                               f"is already recorded (entry #{existing[0]}).",
                }), 409

        cheque_date_iso  = parse_iso_date(data.get("cheque_date_iso"))
        deposit_due_date = parse_iso_date(data.get("deposit_due_date")) or cheque_date_iso
        amount_value     = parse_amount(data.get("amount_numbers"))

        cur.execute(
            """
            INSERT INTO cheques
                (bank_name, account_number, cheque_number, cheque_date, cheque_date_iso,
                 deposit_due_date, payee, amount_words, amount_numbers, amount_value,
                 issuer_name, status, sales_name, sales_email, location, plant,
                 accounts_email, bh_name, bh_email, cheque_location, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            RETURNING id
            """,
            (
                data.get("bank_name"), account_number, cheque_number,
                data.get("date"), cheque_date_iso, deposit_due_date,
                data.get("payee"), data.get("amount_words"), data.get("amount_numbers"),
                amount_value, data.get("issuer_name"),
                data.get("sales_name"), data.get("sales_email"), data.get("location"),
                data.get("plant"), data.get("accounts_email"), data.get("bh_name"),
                data.get("bh_email"), data.get("cheque_location") or "Customer",
            ),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        conn.rollback()
        # Unique-index violation fallback
        if "uq_cheque_acct_no" in str(e):
            return jsonify({"error": "duplicate",
                            "message": "This cheque is already recorded."}), 409
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── Routes: report + lifecycle actions ───────────────────────────────────────

REPORT_COLS = [
    "id", "bank_name", "account_number", "cheque_number", "payee",
    "amount_numbers", "amount_value", "cheque_date_iso", "deposit_due_date",
    "status", "sales_name", "location", "plant", "bh_name", "cheque_location",
]

CHEQUE_LOCATIONS = ["Customer", "RDC Accounts", "RDC Sales"]


@app.route("/cheque/<int:cid>/location", methods=["POST"])
def set_location(cid):
    d = request.get_json(silent=True) or {}
    loc = (d.get("cheque_location") or "").strip()
    if loc not in CHEQUE_LOCATIONS:
        return jsonify({"error": f"cheque_location must be one of {CHEQUE_LOCATIONS}"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE cheques SET cheque_location=%s, updated_at=NOW() WHERE id=%s", (loc, cid))
        if cur.rowcount == 0:
            cur.close()
            return jsonify({"error": "Cheque not found"}), 404
        conn.commit()
        cur.close()
        return jsonify({"success": True, "cheque_location": loc})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


def _fetch_pending():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {', '.join(REPORT_COLS)}
            FROM cheques
            WHERE status IN ('PENDING','BOUNCED')
            ORDER BY deposit_due_date NULLS LAST, id
            """
        )
        rows = [dict(zip(REPORT_COLS, r)) for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()
    return rows


@app.route("/report")
def report():
    rows = _fetch_pending()
    today = date.today()
    soon = today + timedelta(days=2)

    overdue, due_soon, upcoming, undated = [], [], [], []
    for r in rows:
        due = r["deposit_due_date"]
        # stale: Indian cheque invalid 3 months after cheque date
        cd = r["cheque_date_iso"]
        r["stale"] = bool(cd and (today - cd).days > 90)
        if due is None:
            undated.append(r)
        elif due < today:
            overdue.append(r)
        elif due <= soon:
            due_soon.append(r)
        else:
            upcoming.append(r)

    def total(group):
        return sum(x["amount_value"] or 0 for x in group)

    groups = [
        ("Crossed deposit date (Overdue)", "overdue", overdue),
        ("Due within 2 days", "soon", due_soon),
        ("Upcoming", "upcoming", upcoming),
        ("No due date set", "undated", undated),
    ]
    return render_template("report.html", groups=groups, total=total,
                           today=today.isoformat(), locations=CHEQUE_LOCATIONS)


@app.route("/cheque/<int:cid>/deposit", methods=["POST"])
def mark_deposit(cid):
    d = request.get_json(silent=True) or {}
    dep_date = parse_iso_date(d.get("date")) or date.today()
    bank = (d.get("bank") or "").strip() or None
    ref  = (d.get("reference") or "").strip() or None
    return _transition(cid, "DEPOSITED", action="DEPOSITED",
                       action_date=dep_date, bank=bank, reference=ref,
                       extra_sql="deposited_date=%s, deposit_bank=%s, deposit_reference=%s",
                       extra_vals=(dep_date, bank, ref))


@app.route("/cheque/<int:cid>/redeposit", methods=["POST"])
def mark_redeposit(cid):
    d = request.get_json(silent=True) or {}
    dep_date = parse_iso_date(d.get("date")) or date.today()
    bank = (d.get("bank") or "").strip() or None
    ref  = (d.get("reference") or "").strip() or None
    return _transition(cid, "DEPOSITED", action="REDEPOSITED",
                       action_date=dep_date, bank=bank, reference=ref,
                       extra_sql="deposited_date=%s, deposit_bank=%s, deposit_reference=%s, "
                                 "bounce_date=NULL, bounce_reason=NULL",
                       extra_vals=(dep_date, bank, ref))


@app.route("/cheque/<int:cid>/clear", methods=["POST"])
def mark_clear(cid):
    d = request.get_json(silent=True) or {}
    cdate = parse_iso_date(d.get("date")) or date.today()
    return _transition(cid, "CLEARED", action="CLEARED", action_date=cdate,
                       extra_sql="cleared_date=%s", extra_vals=(cdate,))


@app.route("/cheque/<int:cid>/bounce", methods=["POST"])
def mark_bounce(cid):
    d = request.get_json(silent=True) or {}
    bdate = parse_iso_date(d.get("date")) or date.today()
    reason = (d.get("reason") or "").strip() or None
    return _transition(cid, "BOUNCED", action="BOUNCED", action_date=bdate, reason=reason,
                       extra_sql="bounce_date=%s, bounce_reason=%s", extra_vals=(bdate, reason))


def _transition(cid, new_status, action, action_date,
                bank=None, reference=None, reason=None,
                extra_sql="", extra_vals=()):
    conn = get_db()
    try:
        cur = conn.cursor()
        set_clause = "status=%s, updated_at=NOW()"
        vals = [new_status]
        if extra_sql:
            set_clause += ", " + extra_sql
            vals.extend(extra_vals)
        vals.append(cid)
        cur.execute(f"UPDATE cheques SET {set_clause} WHERE id=%s", vals)
        if cur.rowcount == 0:
            cur.close()
            return jsonify({"error": "Cheque not found"}), 404
        cur.execute(
            """INSERT INTO cheque_events (cheque_id, action, action_date, bank, reference, reason)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (cid, action, action_date, bank, reference, reason),
        )
        conn.commit()
        cur.close()
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# ── Routes: Excel export ─────────────────────────────────────────────────────

@app.route("/export")
def export():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, bank_name, account_number, cheque_number, cheque_date,
                   cheque_date_iso, deposit_due_date, payee, amount_words,
                   amount_numbers, amount_value, issuer_name, status,
                   deposited_date, deposit_bank, deposit_reference,
                   cleared_date, bounce_date, bounce_reason, cheque_location,
                   sales_name, sales_email, location, plant, bh_name,
                   TO_CHAR(scanned_at AT TIME ZONE 'Asia/Kolkata', 'DD-Mon-YYYY HH24:MI')
            FROM cheques
            ORDER BY COALESCE(deposit_due_date, cheque_date_iso) DESC NULLS LAST, id DESC
            """
        )
        rows = cur.fetchall()
        cur.close()
    finally:
        conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cheques"

    HEADERS = [
        "#", "Bank", "Account No", "Cheque No", "Cheque Date (raw)",
        "Cheque Date", "Deposit Due", "Pay To", "Amount (Words)",
        "Amount (text)", "Amount (₹)", "Issuer", "Status",
        "Deposited On", "Deposit Bank", "Deposit Ref",
        "Cleared On", "Bounced On", "Bounce Reason", "Cheque Location",
        "Sales Name", "Sales Email", "Location", "Plant", "BH Name", "Scanned At (IST)",
    ]

    hdr_font  = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="4F46E5")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin      = Side(style="thin", color="CCCCCC")
    border    = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(HEADERS, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font, c.fill, c.alignment, c.border = hdr_font, hdr_fill, hdr_align, border
    ws.row_dimensions[1].height = 30

    alt = PatternFill("solid", fgColor="F5F3FF")
    for ri, row in enumerate(rows, 2):
        shade = alt if ri % 2 == 0 else None
        for ci, val in enumerate(row, 1):
            c = ws.cell(row=ri, column=ci, value=val if val is not None else "")
            c.border = border
            c.alignment = Alignment(vertical="center")
            if shade:
                c.fill = shade

    widths = [6, 16, 16, 11, 14, 12, 12, 20, 26, 14, 13, 18, 11,
              12, 16, 14, 11, 11, 20, 14, 18, 22, 14, 12, 18, 20]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="cheques.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
