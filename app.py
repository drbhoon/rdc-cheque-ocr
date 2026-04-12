import os
import uuid
import base64
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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


PROMPT = """This is an image of an Indian vehicle (truck/lorry) captured from a CCTV or camera.
Find the registration (number) plate and read it carefully — the image may be blurry or low resolution.

Indian plates follow this format: 2-letter state code + 2-digit district + 1-2 letter series + 4-digit number.
Example: MH12AB1234, OR02BU3389, KA51MX4567.
The plate may be printed on two lines — read both lines combined as one number.

Reply with ONLY the plate number, no spaces, no punctuation, no explanation.
If no plate is visible at all, reply with NONE."""


def read_plate(img_bytes, media_type="image/jpeg"):
    img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=32,
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
    raw = message.content[0].text.strip().upper()
    text = "".join(c for c in raw if c.isalnum())
    return text if text and text != "NONE" else None


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
        text = read_plate(img_bytes, media_type)
        if text:
            return jsonify({"plates": [{"text": text}], "count": 1})
        else:
            return jsonify({"plates": [], "count": 0})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
