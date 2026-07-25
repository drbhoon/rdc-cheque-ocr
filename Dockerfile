# syntax=docker/dockerfile:1
# ── RDC PDC Cheque Tracker — production image (Flask + Gunicorn) ─────────────
FROM python:3.11-slim

# Clean, predictable Python in containers
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching. psycopg2-binary and the
# rest ship manylinux wheels, so no system build tools are required.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code (app.py, templates/, …)
COPY . .

# Internal container port the app listens on (matches the published host port)
EXPOSE 3001

# Gunicorn: 2 workers x 4 threads, 120s timeout (Claude OCR can take a few seconds).
# --max-requests recycles each worker after ~800 requests (+/- jitter) so any
# slow resource leak is reset periodically; --graceful-timeout bounds shutdown.
# The app creates/migrates its own Postgres tables on startup.
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:3001", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--max-requests", "800", \
     "--max-requests-jitter", "100", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
