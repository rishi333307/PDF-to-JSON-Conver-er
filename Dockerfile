# ---------------------------------------------------------------------------
# PDF-to-JSON service container.
#
# Bundles everything the app needs to run identically on any machine:
#   - Python 3.11
#   - Tesseract OCR engine (for scanned/image pages)
#   - Ghostscript (required by Camelot's table extraction)
#   - Poppler utils (required by pdf2image, which feeds Tesseract)
#   - OpenCV system libraries (required by camelot-py[cv])
#
# Build:  docker build -t pdf-to-json .
# Run:    docker run -p 5000:5000 pdf-to-json
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# Avoid interactive prompts during apt installs, keep image layers thin.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# --- System dependencies ---
# tesseract-ocr            -> OCR engine itself
# tesseract-ocr-eng        -> English language data (add more tesseract-ocr-<lang> as needed)
# ghostscript              -> required by Camelot for PDF table extraction
# poppler-utils            -> required by pdf2image (renders PDF pages to images for OCR)
# libgl1, libglib2.0-0     -> runtime libraries required by opencv-python (camelot-py[cv])
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        ghostscript \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (separate layer = better Docker caching
# — code changes won't force a full reinstall of every library).
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt gunicorn

# Now copy the rest of the application.
COPY backend ./backend
COPY frontend ./frontend

# Folders the app writes to at runtime.
RUN mkdir -p /app/uploads /app/outputs

EXPOSE 5000

# Health check so `docker ps` and orchestrators (Railway, Oracle Cloud,
# Kubernetes, etc.) know whether the container is actually serving
# requests, not just running.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT', '5000') + '/health', timeout=3)" || exit 1

WORKDIR /app/backend

# gunicorn instead of Flask's dev server: handles concurrent requests
# properly and won't crash the whole process on one bad request.
#
# Stays at a SINGLE gunicorn worker by default, and that's intentional
# -- NOT a limitation to "fix" by raising WEB_CONCURRENCY. The actual
# parallel PDF processing now happens inside app.py itself, via a
# dedicated process pool (pdf_worker_pool) that's sized automatically
# at startup based on this container's REAL available RAM/CPU (see
# concurrency_limit.py + cpu_detect.py). That pool is the right place
# for this concurrency: it's created once and reused for every request,
# so the ~150-200MB cost of importing camelot/opencv is paid once per
# pool worker, not once per gunicorn worker.
#
# Raising WEB_CONCURRENCY would spawn ADDITIONAL full copies of this
# Flask app (each with its OWN separate pdf_worker_pool), multiplying
# that import cost again for no real benefit -- gunicorn's job here is
# just to stay responsive for HTTP I/O while the pool does the actual
# CPU-heavy work in the background, and one worker is enough for that.
# --timeout is generous because large/scanned PDFs with many pages of
# OCR -- or a big batch via /upload-batch queued behind a small pool --
# can legitimately take a while.
CMD gunicorn --bind 0.0.0.0:${PORT:-5000} \
    --workers ${WEB_CONCURRENCY:-1} \
    --timeout ${GUNICORN_TIMEOUT:-300} \
    app:app
