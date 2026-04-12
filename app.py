import os
import uuid
import base64
import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}

_detector = None
_anthropic_client = None


def get_detector():
    global _detector
    if _detector is None:
        from open_image_models import LicensePlateDetector
        _detector = LicensePlateDetector(
            detection_model="yolo-v9-t-384-license-plate-end2end",
            conf_thresh=0.20,
        )
    return _detector


def get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
        _anthropic_client = anthropic.Anthropic(api_key=api_key)
    return _anthropic_client


def ocr_plate_with_claude(crop_bgr):
    """Send a plate crop to Claude Haiku and get back the plate number text."""
    # Encode the crop as JPEG base64
    success, buf = cv2.imencode(".jpg", crop_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not success:
        return None, 0.0
    img_b64 = base64.standard_b64encode(buf.tobytes()).decode("utf-8")

    client = get_anthropic_client()
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "This is a vehicle license plate. "
                            "Read the plate number exactly as printed. "
                            "Reply with ONLY the alphanumeric plate number, no spaces, no punctuation, no explanation."
                        ),
                    },
                ],
            }
        ],
    )
    text = message.content[0].text.strip().upper()
    # Strip any stray punctuation/spaces just in case
    text = "".join(c for c in text if c.isalnum())
    return text


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

    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Use PNG, JPG, JPEG, WEBP, or BMP"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        img = cv2.imread(filepath)
        if img is None:
            return jsonify({"error": "Could not read image file"}), 400

        ih, iw = img.shape[:2]

        # Step 1: detect plate bounding boxes with local YOLO model
        detector = get_detector()
        detections = detector.predict(filepath)

        plates = []

        if detections:
            # Step 2a: crop each detected plate and OCR with Claude
            for det in detections:
                box = det.bounding_box
                x1 = max(0, int(box.x1))
                y1 = max(0, int(box.y1))
                x2 = min(iw, int(box.x2))
                y2 = min(ih, int(box.y2))

                crop = img[y1:y2, x1:x2]
                if crop.size == 0:
                    continue

                text = ocr_plate_with_claude(crop)
                if text:
                    plates.append({
                        "text": text,
                        "detection_confidence": round(float(det.confidence), 3),
                    })
        else:
            # Step 2b: no plate box found — send full image to Claude anyway
            text = ocr_plate_with_claude(img)
            if text:
                plates.append({
                    "text": text,
                    "detection_confidence": None,
                })

        return jsonify({"plates": plates, "count": len(plates)})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

    finally:
        if os.path.exists(filepath):
            os.remove(filepath)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
