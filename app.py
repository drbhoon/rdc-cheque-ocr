import os
import re
import json
import base64
import io
from flask import Flask, request, jsonify, render_template, send_file
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}

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
    """Return a psycopg2-compatible connection URL."""
    url = os.environ.get("DATABASE_URL", "")
    # Railway supplies postgres:// — psycopg2 needs postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_db():
    import psycopg2
    return psycopg2.connect(db_url())


def init_db():
    """Create the cheques table if it does not exist."""
    conn = get_db()
    try:
        cur = conn.cursor()
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
        conn.commit()
        cur.close()
    finally:
        conn.close()


# Initialise on startup (works with both `python app.py` and gunicorn)
with app.app_context():
    try:
        init_db()
    except Exception as _e:
        print(f"[DB] init skipped: {_e}")


# ── Claude prompt ─────────────────────────────────────────────────────────────

PROMPT = """You are scanning an Indian bank cheque image. Extract exactly these 7 fields and return them as a valid JSON object.

Fields:
1. bank_name       — Bank name printed on the cheque (e.g. "ICICI Bank")
2. account_number  — Account number from the A/c No. field (digits only, no spaces)
3. date            — Date written on the cheque exactly as shown (e.g. "14/7/22")
4. payee           — Name in the "Pay" field (who the cheque is made out to)
5. amount_words    — Amount in the "Rupees" line written in words (e.g. "Five Lakhs Only")
6. amount_numbers  — Amount in the numeric box (e.g. "5,00,000")
7. issuer_name     — Account holder / drawer name printed at the bottom of the cheque

Rules:
- Return ONLY a raw JSON object, no markdown fences, no explanation.
- Use null for any field that is not visible or unreadable.
- Do not invent or guess values — use null if uncertain.

Example output:
{"bank_name":"ICICI Bank","account_number":"015601500005","date":"14/7/22","payee":"RDC Concrete","amount_words":"Five Lakhs Only","amount_numbers":"5,00,000","issuer_name":"BRIG K S BHOON"}"""


def read_cheque(img_bytes, media_type="image/jpeg"):
    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": img_b64},
                    },
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw.strip())


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/predict", methods=["POST"])
def predict():
    if "image" not in request.files:
        return jsonify({"error": "No image uploaded"}), 400
    file = request.files["image"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Unsupported file type"}), 400
    img_bytes = file.read()
    media_type = "image/jpeg" if ext in {"jpg", "jpeg"} else f"image/{ext}"
    try:
        data = read_cheque(img_bytes, media_type)
        return jsonify({"cheque": data})
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Could not parse cheque data: {e}"}), 500
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/accept", methods=["POST"])
def accept():
    """Save reviewed cheque data to the database."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data received"}), 400
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cheques
                (bank_name, account_number, cheque_date, payee,
                 amount_words, amount_numbers, issuer_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                data.get("bank_name"),
                data.get("account_number"),
                data.get("date"),
                data.get("payee"),
                data.get("amount_words"),
                data.get("amount_numbers"),
                data.get("issuer_name"),
            ),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/export")
def export():
    """Download all cheque records as a formatted Excel file."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, bank_name, account_number, cheque_date, payee,
                   amount_words, amount_numbers, issuer_name,
                   TO_CHAR(scanned_at AT TIME ZONE 'Asia/Kolkata', 'DD-Mon-YYYY HH24:MI') AS scanned_at
            FROM cheques
            ORDER BY scanned_at DESC
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
        "#", "Bank Name", "Account Number", "Date",
        "Pay To", "Amount (Words)", "Amount (₹)", "Issuer Name", "Scanned At (IST)"
    ]

    # Header styling
    hdr_font  = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="4F46E5")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin      = Side(style="thin", color="CCCCCC")
    border    = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col, h in enumerate(HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font  = hdr_font
        cell.fill  = hdr_fill
        cell.alignment = hdr_align
        cell.border = border

    ws.row_dimensions[1].height = 28

    # Data rows — alternate row shading
    alt_fill = PatternFill("solid", fgColor="F5F3FF")
    for r_idx, row in enumerate(rows, 2):
        fill = alt_fill if r_idx % 2 == 0 else None
        for c_idx, val in enumerate(row, 1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val if val is not None else "")
            cell.border = border
            cell.alignment = Alignment(vertical="center")
            if fill:
                cell.fill = fill

    # Auto column widths
    col_widths = [6, 20, 18, 12, 24, 30, 16, 22, 22]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    ws.freeze_panes = "A2"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="cheques.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
