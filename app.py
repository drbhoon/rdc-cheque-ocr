import os
import re
import json
import base64
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}

_client = None


def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


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
    # Strip markdown code fences if Claude wraps the response
    raw = re.sub(r"^```[a-z]*\n?", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw, flags=re.MULTILINE)
    return json.loads(raw.strip())


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
