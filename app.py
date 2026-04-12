import os
import uuid
import json
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB max upload

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}

# Lazy-load ALPR so startup is fast
_alpr = None

def get_alpr():
    global _alpr
    if _alpr is None:
        from fast_alpr import ALPR
        _alpr = ALPR(
            detector_model="yolo-v9-t-384-license-plate-end2end",
            ocr_model="cct-xs-v2-global-model",
        )
    return _alpr


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

    # Save with unique name to avoid collisions
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)

    try:
        alpr = get_alpr()
        results = alpr.predict(filepath)

        plates = []
        for r in results:
            if r.ocr and r.ocr.text:
                plates.append({
                    "text": r.ocr.text,
                    "ocr_confidence": round(
                        float(r.ocr.confidence) if isinstance(r.ocr.confidence, (int, float))
                        else float(sum(r.ocr.confidence) / len(r.ocr.confidence)),
                        3,
                    ),
                    "detection_confidence": round(float(r.detection.confidence), 3),
                })

        return jsonify({
            "plates": plates,
            "count": len(plates),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # Clean up uploaded file
        if os.path.exists(filepath):
            os.remove(filepath)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
