"""
PDF to JSON Converter - Backend Server
----------------------------------------
This version classifies EVERY PAGE individually as one of:

    "text"       -> page has a real, selectable text layer, no raster image
    "image"      -> page has no real text layer, only raster image(s) (scanned look)
    "mixed"      -> page has BOTH a real text layer AND raster image(s)
    "empty"      -> page has neither (blank page)

Detection is done with PyMuPDF (fitz), which is the most reliable and
fastest way to check, per page, whether a real text layer exists and
whether raster images are embedded. If PyMuPDF is not installed, the
app automatically falls back to pdfplumber for the same classification
(slightly slower, still accurate).

Classification always runs first, for every page, and DRIVES which
extractor is used on that specific page:

    - "text"  pages  -> Camelot is tried first (in case the page holds
                        a real table); if no real table is found, plain
                        pdfplumber text extraction is used instead
    - "mixed" pages  -> same as "text" (a real text layer is already
                        present, so OCR is unnecessary even though an
                        image also sits on the page; the image is still
                        flagged in the JSON so the user knows it's there)
    - "image" pages  -> OCR (Tesseract), only if OCR libraries are
                        installed; otherwise the page is reported with
                        empty data and "ocr_unavailable" as the method
    - "empty" pages  -> reported as empty, nothing to extract

Earlier versions of this app ran Camelot once on the WHOLE document
before classifying pages at all. That caused two bugs: (1) Camelot's
borderless "stream" mode sometimes misread normal paragraphs as a fake
table, hijacking text extraction with garbled output, and (2)
image/mixed pages were silently skipped since Camelot can't read them,
with no fallback. Running classification first, per page, and only
trying Camelot on pages that actually have a text layer (with strict
validation that a real table was found) fixes both issues.

No AI APIs are used anywhere. Only traditional parsing/OCR libraries.
"""

import os
import json
import re
import time
import uuid
import hashlib
from collections import Counter
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# RamBudgetPool replaces the older flat-worker-count approach
# (concurrency_limit.py, still kept in the repo for reference/rollback)
# with per-PDF cost estimation: instead of deciding ONE worker count
# for every PDF regardless of size, it estimates EACH PDF's likely RAM
# cost (pdf_cost_estimator.py) and admits work against a live RAM
# budget. A folder of small PDFs can run more of them concurrently than
# a folder of large scanned ones -- the system adapts per-file instead
# of assuming every PDF costs the same. See ram_budget_pool.py for the
# full reasoning.
from ram_budget_pool import RamBudgetPool, as_completed_ram_budget

# balance_into_batches() implements the same weight-balanced splitting
# algorithm the benchmark script uses, now shared via batch_balancer.py
# so /upload-batch-job (below) and benchmark.py can never drift apart.
from batch_balancer import balance_into_batches

# --- PDF processing libraries ---
import camelot          # table extraction
import pdfplumber       # text extraction + per-page image fallback detection

# PyMuPDF is the primary engine for per-page text/image classification.
# It is fast and very accurate. If it's missing, we fall back to a
# pdfplumber/pdfminer-based check further below.
try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

# OCR is OPTIONAL. If pytesseract / pdf2image / Tesseract binary are not
# installed, the app still works fine for text/mixed pages — it just
# can't OCR pure-image pages (it will say so in the result instead).
try:
    import pytesseract
    from pdf2image import convert_from_path
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False


# ---------------------------------------------------------------------------
# Basic Flask setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)  # allow API calls from any origin (needed for other tools/scripts calling this as a public API)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "..", "uploads")
OUTPUT_FOLDER = os.path.join(BASE_DIR, "..", "outputs")

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

ALLOWED_EXTENSION = ".pdf"

# Minimum number of real text characters on a page before we trust the
# text layer. PDFs sometimes carry a few stray/garbage characters (e.g.
# a watermark glyph) even on a fully scanned page — a low threshold
# avoids misclassifying those as "has real text".
MIN_TEXT_CHARS_FOR_TEXT_PAGE = 3

# --- API / URL-fetch configuration ---
# Caps how large a file this service will accept, whether it arrives as
# a direct upload or is downloaded from a URL. Keeps one bad request
# (a huge file, or a URL pointing at something enormous) from chewing
# through memory/disk before this code even gets a chance to run.
#
# Raised from an original 50MB default to 500MB: 50MB was sized for the
# smallest possible hosting tier (e.g. Railway's free plan) and proved
# too tight for realistic multi-file batches -- a batch of even 10-15
# moderate office PDFs, or a single large scanned document, could
# exceed it on its own. This is intentionally still just a blunt,
# request-level backstop, not the real protection against memory
# exhaustion -- that finer-grained job belongs to RamBudgetPool
# (ram_budget_pool.py), which checks ACTUAL live available RAM against
# each PDF's own estimated cost at admission time. This cap's only
# remaining job is to stop one absurdly oversized request from being
# accepted at all, before RamBudgetPool ever gets a chance to look at
# it file-by-file. Lower this back down (e.g. to 20-50MB) if deploying
# on a memory-constrained free/hobby tier; raise it further if your
# real server has the RAM to match.
MAX_PDF_BYTES = int(os.environ.get("MAX_PDF_BYTES", 500 * 1024 * 1024))  # 500 MB default
URL_FETCH_TIMEOUT_SECONDS = int(os.environ.get("URL_FETCH_TIMEOUT_SECONDS", 30))
# NOTE: this caps the TOTAL size of a request body. For /upload-batch,
# that means the SUM of every file in the batch must stay under this
# limit, not just each individual file -- a batch of 10 files at 10MB
# each (100MB total) needs MAX_PDF_BYTES raised accordingly, the same
# way it would for one 100MB single-file upload today.
app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_BYTES

# Caps how many files a single /upload-batch request can submit at
# once. This is deliberately separate from the RAM budget pool's live
# admission limit (which caps how many run AT THE SAME TIME, and now
# varies per-batch based on each file's own estimated cost) -- a batch
# can still be larger than however many fit in the budget at any one
# moment; the extra files just queue and get admitted as budget frees
# as soon as one frees up. This cap exists purely to stop one request
# from queueing an unreasonable number of files (e.g. someone
# accidentally selecting their entire Downloads folder), which would
# otherwise tie up the pool for a very long time before any OTHER
# request (including other users' batches) gets a turn.
MAX_FILES_PER_BATCH = int(os.environ.get("MAX_FILES_PER_BATCH", 20))

# --- Disk cleanup configuration ---
# On a public deployment, every request (from every user) leaves a PDF
# in uploads/ and a JSON file in outputs/. Without cleanup, disk usage
# grows forever. Two cleanup strategies run together:
#
#   1. /api/process deletes its own output files immediately after
#      sending the response — that endpoint hands the data back
#      directly in the JSON body, so the on-disk copy is redundant the
#      moment the response is sent. (See api_process() below.)
#
#   2. /upload (the web frontend) keeps files around for a while so the
#      "Download JSON" button keeps working after the page finishes
#      loading — but a background sweeper thread deletes anything
#      older than FILE_RETENTION_HOURS, so nothing accumulates forever.
FILE_RETENTION_HOURS = float(os.environ.get("FILE_RETENTION_HOURS", 6))
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", 1800))  # 30 min


# ---------------------------------------------------------------------------
# Parallel PDF processing pool
# ---------------------------------------------------------------------------
# WHY A PROCESS POOL (not a thread pool):
#   Measured directly on this project's own extraction code: Camelot's
#   per-page table detection does NOT meaningfully release Python's GIL
#   during its CPU-bound work, so threads gave ~1.0x speedup (i.e. no
#   real parallelism) when processing pages concurrently. Separate OS
#   processes, which each get genuine CPU time, gave real speedup --
#   but only on a machine with more than 1 CPU core actually available.
#
# WHY THE POOL IS SIZED THIS WAY (not a hardcoded number):
#   Each worker process pays a one-time ~150-200MB import cost just for
#   loading camelot/opencv, BEFORE it even opens a PDF. That cost is
#   paid ONCE per worker process for the worker's entire lifetime, not
#   once per PDF -- which is exactly why this pool is created once at
#   module load and reused for every request, instead of spinning up a
#   fresh process per upload. get_max_concurrent_pdfs() (see
#   concurrency_limit.py) looks at REAL available RAM and REAL available
#   CPU cores on whatever machine this happens to run on -- correctly
#   shrinking to 1 on a tiny Render free-tier box, and correctly growing
#   on a bigger machine, with no code changes required either way.
#
# WHY GUNICORN STAYS AT 1 WEB WORKER (see Dockerfile/start.sh):
#   gunicorn's --workers setting ALSO spawns separate OS processes, each
#   one a full copy of this Flask app (and therefore ALSO paying the
#   camelot import cost independently). Stacking gunicorn workers on top
#   of this PDF-processing pool would multiply that import cost again
#   for no benefit -- the HTTP server itself is lightweight I/O work and
#   doesn't need multiple processes to stay responsive while this pool
#   does the actual heavy lifting in the background.
pdf_worker_pool = RamBudgetPool()


def delete_file_quietly(path):
    """Deletes a file if it exists; never raises (cleanup should never
    crash a request or the sweeper thread over a missing/locked file)."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def safe_unique_upload_path(original_filename):
    """
    Builds an on-disk path for a saved upload that can never collide
    with another file, even when multiple uploads (with the SAME
    original filename, e.g. two different users both uploading
    "report.pdf") are being saved and processed at the same time by
    different worker processes.

    Previously, the on-disk path was just UPLOAD_FOLDER + the original
    filename verbatim. That was always a latent bug (two simultaneous
    uploads of "report.pdf" would overwrite each other on disk), but it
    was only a LATENT one when every request was handled one at a time.
    Now that uploads can be processed several at once via
    pdf_worker_pool, a same-named collision is no longer a rare edge
    case -- it's something a batch upload of similarly-named files
    (e.g. several "scan.pdf" exports from different folders) will hit
    immediately and often.

    Keeps the original filename's extension and a short slug of the
    original name (purely for human-readability if someone looks in the
    uploads/ folder), but the part that actually guarantees uniqueness
    is the uuid4 segment.
    """
    original_filename = original_filename or "upload.pdf"
    base = os.path.splitext(os.path.basename(original_filename))[0]
    # Strip anything that isn't a safe filename character, so this also
    # closes off path-traversal-via-filename as a side effect (e.g. a
    # filename of "../../etc/something.pdf" becomes just "something").
    safe_base = re.sub(r"[^A-Za-z0-9_-]+", "_", base)[:60] or "upload"
    unique_id = uuid.uuid4().hex[:12]
    unique_filename = f"{safe_base}_{unique_id}{ALLOWED_EXTENSION}"
    return os.path.join(UPLOAD_FOLDER, unique_filename)


def cleanup_old_files(folder, max_age_hours):
    """
    Deletes every file in `folder` whose last-modified time is older
    than max_age_hours. Used both by the periodic background sweep and
    by an immediate cleanup pass on server startup (so leftover files
    from a previous run/deploy don't linger indefinitely).
    """
    if max_age_hours <= 0:
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0
    try:
        for name in os.listdir(folder):
            if name.startswith("."):  # skip .gitkeep etc.
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    deleted += 1
            except OSError:
                continue
    except OSError:
        pass
    return deleted


def start_cleanup_sweeper():
    """
    Background daemon thread that periodically deletes old files from
    both uploads/ and outputs/. Runs for the lifetime of the server
    process. A daemon thread is used so it never blocks the app from
    shutting down.
    """
    import threading

    def sweep_loop():
        while True:
            removed_uploads = cleanup_old_files(UPLOAD_FOLDER, FILE_RETENTION_HOURS)
            removed_outputs = cleanup_old_files(OUTPUT_FOLDER, FILE_RETENTION_HOURS)
            if removed_uploads or removed_outputs:
                print(f"[cleanup] Removed {removed_uploads} old upload(s), "
                      f"{removed_outputs} old output(s) (older than {FILE_RETENTION_HOURS}h).")
            time.sleep(CLEANUP_INTERVAL_SECONDS)

    thread = threading.Thread(target=sweep_loop, daemon=True)
    thread.start()


# Run one cleanup pass immediately on startup (catches anything left
# over from before a restart/redeploy), then start the periodic sweeper.
cleanup_old_files(UPLOAD_FOLDER, FILE_RETENTION_HOURS)
cleanup_old_files(OUTPUT_FOLDER, FILE_RETENTION_HOURS)
start_cleanup_sweeper()


class PdfFetchError(Exception):
    """Raised when a PDF can't be downloaded/validated from a URL. The
    message is safe to show directly to an API caller."""
    pass


def download_pdf_from_url(url):
    """
    Downloads a PDF from a plain, direct link (e.g. https://host/file.pdf)
    and saves it into UPLOAD_FOLDER, the same place an uploaded file would
    land — so it can be handed to extract_pdf_data() exactly the same way.

    Deliberately simple: this expects the URL to point straight at PDF
    bytes. It does NOT special-case Google Drive "share" links, Dropbox
    share pages, login-gated URLs, etc. — those return an HTML page, not
    a PDF, and will be rejected by the content checks below.

    Raises PdfFetchError with a human-readable reason on any failure.
    Returns the local file path of the downloaded PDF on success.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise PdfFetchError("URL must start with http:// or https://")

    try:
        response = requests.get(
            url,
            timeout=URL_FETCH_TIMEOUT_SECONDS,
            stream=True,
            headers={"User-Agent": "pdf-to-json-service/1.0"},
        )
    except requests.exceptions.RequestException as e:
        raise PdfFetchError(f"Could not reach the URL: {e}")

    if response.status_code != 200:
        raise PdfFetchError(f"URL returned HTTP {response.status_code}, expected 200.")

    # Reject obviously-non-PDF responses early using the Content-Length
    # header when present, before downloading the whole body.
    content_length = response.headers.get("Content-Length")
    if content_length is not None and int(content_length) > MAX_PDF_BYTES:
        raise PdfFetchError(
            f"File is {int(content_length)} bytes, which exceeds the "
            f"{MAX_PDF_BYTES} byte limit for this service."
        )

    # Stream to disk with a running size check, so a server that lies
    # about Content-Length (or omits it) can't exhaust memory/disk.
    unique_name = f"url_{uuid.uuid4().hex[:12]}.pdf"
    dest_path = os.path.join(UPLOAD_FOLDER, unique_name)
    total_bytes = 0
    try:
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if total_bytes > MAX_PDF_BYTES:
                    raise PdfFetchError(
                        f"File exceeds the {MAX_PDF_BYTES} byte limit for this service "
                        f"(download aborted partway through)."
                    )
                f.write(chunk)
    except PdfFetchError:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise
    except OSError as e:
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise PdfFetchError(f"Could not save downloaded file: {e}")

    if total_bytes == 0:
        os.remove(dest_path)
        raise PdfFetchError("Downloaded file was empty.")

    # Validate it's actually a PDF by checking the file signature ("%PDF")
    # rather than trusting the Content-Type header, which is often wrong
    # or missing on misconfigured servers.
    with open(dest_path, "rb") as f:
        header = f.read(5)
    if not header.startswith(b"%PDF"):
        os.remove(dest_path)
        raise PdfFetchError(
            "The URL did not return a PDF file (no %PDF signature found — "
            "this often happens with Google Drive/Dropbox 'share' links, "
            "which return an HTML viewer page rather than the raw file)."
        )

    return dest_path


# ---------------------------------------------------------------------------
# Per-page classification: does this page have text, an image, both, or neither?
# ---------------------------------------------------------------------------
def classify_pages_pymupdf(pdf_path):
    """
    Preferred method. Uses PyMuPDF to check, for every page:
      - get_text() -> is there a real text layer with enough characters?
      - get_images() -> are there one or more raster images placed on the page?

    Returns a list of dicts:
      [{"page": 1, "has_text": True, "has_image": False, "image_count": 0}, ...]
    """
    classifications = []
    doc = fitz.open(pdf_path)
    try:
        for page_index in range(len(doc)):
            page = doc[page_index]

            raw_text = page.get_text("text") or ""
            stripped_len = len(raw_text.strip())
            has_text = stripped_len >= MIN_TEXT_CHARS_FOR_TEXT_PAGE

            # get_images(full=True) lists every raster image XObject used
            # by the page, including ones reused from elsewhere in the
            # PDF. This correctly catches scanned pages (one full-page
            # image) AND pages with smaller embedded photos/figures.
            images_on_page = page.get_images(full=True)
            has_image = len(images_on_page) > 0

            classifications.append({
                "page": page_index + 1,
                "has_text": has_text,
                "has_image": has_image,
                "image_count": len(images_on_page),
            })
    finally:
        doc.close()

    return classifications


def classify_pages_pdfplumber(pdf_path):
    """
    Fallback method if PyMuPDF isn't installed. Uses pdfplumber to check:
      - page.chars -> individual text glyphs actually positioned on the page
        (more reliable than extract_text() alone, since extract_text() can
        sometimes return whitespace-only strings for near-empty pages)
      - page.images -> raster images placed on the page

    Returns the same list-of-dicts shape as classify_pages_pymupdf.
    """
    classifications = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            chars = page.chars or []
            text = (page.extract_text() or "").strip()
            has_text = len(chars) >= MIN_TEXT_CHARS_FOR_TEXT_PAGE and len(text) > 0

            images_on_page = page.images or []
            has_image = len(images_on_page) > 0

            classifications.append({
                "page": page_index + 1,
                "has_text": has_text,
                "has_image": has_image,
                "image_count": len(images_on_page),
            })

    return classifications


def classify_pages(pdf_path):
    """
    Runs the best available classification method.
    """
    if PYMUPDF_AVAILABLE:
        try:
            return classify_pages_pymupdf(pdf_path)
        except Exception as e:
            print(f"[Classify] PyMuPDF failed, falling back to pdfplumber: {e}")

    return classify_pages_pdfplumber(pdf_path)


def page_type_label(has_text, has_image):
    """
    Turns the (has_text, has_image) pair into a single readable label.
    """
    if has_text and has_image:
        return "mixed"      # both real text AND a picture/scan on the same page
    if has_text:
        return "text"       # normal text page, no embedded image
    if has_image:
        return "image"      # scanned/picture page, no real text layer
    return "empty"           # neither — blank page


# ---------------------------------------------------------------------------
# Extraction Step 1: Camelot (tables) — run PER PAGE, only on pages that
# actually have a real text layer ("text" or "mixed"). Camelot cannot read
# image-only pages at all, and running it on the whole document at once
# previously caused two bugs: (1) plain paragraphs on text pages were
# sometimes misread as a borderless "stream" table, hijacking normal text
# extraction with garbled output, and (2) image/mixed pages were silently
# skipped because Camelot can't see them, with nothing to fall back to.
# Running it per page, with strict validation, fixes both.
# ---------------------------------------------------------------------------
def looks_like_real_table(df):
    """
    Guards against Camelot's "stream" mode false-positiving on normal
    paragraph text (it can slice a sentence into fake "columns" based on
    whitespace gaps). A result only counts as a real table if it has at
    least 2 rows AND at least 2 columns, AND most cells actually contain
    short-ish content (real table cells are rarely full sentences).
    """
    if df.shape[0] < 2 or df.shape[1] < 2:
        return False

    total_cells = 0
    long_cells = 0
    for _, row in df.iterrows():
        for cell in row:
            cell_text = str(cell).strip()
            if not cell_text:
                continue
            total_cells += 1
            # A "long" cell (looks like a sentence/paragraph, not a table value)
            if len(cell_text) > 60 or cell_text.count(" ") > 8:
                long_cells += 1

    if total_cells == 0:
        return False

    # If most non-empty cells look like full sentences, this is paragraph
    # text that Camelot misread as a table, not a real table.
    return (long_cells / total_cells) < 0.5


def clean_cell_text(value):
    """
    Cleans a single table cell's text: collapses internal newlines/extra
    whitespace (Camelot often keeps the original line wraps from the PDF)
    into single spaces, and strips leading/trailing whitespace.
    """
    text = str(value)
    # Collapse all whitespace (including literal \n inside the cell) into
    # single spaces, so wrapped PDF lines become one clean sentence.
    text = " ".join(text.split())
    return text.strip()


def header_to_key(header_text):
    """
    Converts a table header like "What Was Seen" or "ID" into a camelCase
    JSON key like "whatWasSeen" or "id". Falls back to "column_N" if the
    header is empty (e.g. a blank header cell).
    """
    cleaned = clean_cell_text(header_text)
    words = [w for w in cleaned.replace("_", " ").split(" ") if w]
    if not words:
        return None

    first = words[0].lower()
    rest = "".join(w[:1].upper() + w[1:].lower() if w else "" for w in words[1:])
    return first + rest


def extract_structured_table_for_page(pdf_path, page_number, known_headers=None):
    """
    Tries Camelot on a single page and, if a real table is found, returns
    it as a list of DICTS (one dict per data row) instead of pipe-joined
    strings — e.g. {"id": "92335", "date": "06.08.2026", "company": "..."}.

    `known_headers` is an optional list of camelCase keys carried over from
    a previous page of the SAME multi-page table (e.g. a report whose
    header row only appears once, on page 1, while later pages continue
    the same columns without repeating the header). When provided, ALL
    rows on this page are treated as data rows using those headers,
    instead of trying to detect a new header row on this page.

    Returns (rows, headers_used, raw_rows):
      rows         -> list of dicts, e.g. [{"id": "92335", ...}, ...],
                      with whitespace-cleaned values
      headers_used -> the camelCase header keys actually used for this
                      page's table (pass this back in as known_headers
                      for the next page of the same table)
      raw_rows     -> same shape as `rows`, but values are NOT whitespace-
                      cleaned (internal newlines from line-wrapped PDF
                      cells are preserved). Needed by
                      resolve_constant_column_leak() to detect and undo
                      a merged-cell leak, which relies on those newlines.
    """
    try:
        tables = camelot.read_pdf(pdf_path, pages=str(page_number), flavor="lattice")
        if tables.n == 0:
            tables = camelot.read_pdf(pdf_path, pages=str(page_number), flavor="stream")
    except Exception as e:
        print(f"[Camelot] Skipped on page {page_number}: {e}")
        return [], known_headers, []

    for table in tables:
        df = table.df
        if not looks_like_real_table(df):
            continue

        raw_rows = [[clean_cell_text(cell) for cell in row] for _, row in df.iterrows()]
        row_width = len(raw_rows[0])

        headers = None
        data_rows = None

        if known_headers:
            if len(known_headers) == row_width:
                # Same column count as the table's first page -> reuse as-is.
                headers = known_headers
                data_rows = raw_rows
            elif row_width < len(known_headers):
                # Camelot sometimes drops fully-empty columns on a later
                # page of the same table (e.g. a "Comments" column with
                # no values on this particular page). Detecting an exact
                # column match isn't reliable here, so instead: treat
                # every row on this page as a DATA row (never a header —
                # a continuation page of a real table doesn't suddenly
                # start with a header), and assign the first N known
                # headers to the columns actually present. This keeps
                # data correctly labeled even when columns are dropped,
                # at the cost of possibly mislabeling which specific
                # column was dropped if it wasn't the last one.
                headers = known_headers[:row_width]
                data_rows = raw_rows

        if headers is None:
            # First page of this table, or more columns appeared than
            # before (unexpected growth, treat as a new table): row 0
            # is the header row.
            header_cells = raw_rows[0]
            headers = [header_to_key(h) or f"column_{i+1}" for i, h in enumerate(header_cells)]
            data_rows = raw_rows[1:]
            header_row_skipped = True
        else:
            header_row_skipped = False

        # Keep a RAW (newline-preserving) version of each row alongside
        # the cleaned one. Camelot can read a tall, visually-merged cell
        # (e.g. a constant rig/badge column spanning several short rows
        # in the PDF) as one cell holding several newline-joined
        # fragments, attached only to the first row of that span. Those
        # newlines are the only reliable signal for where one row's
        # fragment ends and the next begins — clean_cell_text() collapses
        # them into spaces, so resolve_constant_column_leak() (below)
        # needs to run on this raw version BEFORE that collapsing happens,
        # or the fragments become unrecoverable.
        all_raw_rows = [[str(cell) for cell in row] for _, row in df.iterrows()]
        raw_data_rows = all_raw_rows[1:] if header_row_skipped else all_raw_rows

        results = []
        raw_results = []
        for row_cells, raw_row_cells in zip(data_rows, raw_data_rows):
            # Always include every header as a key, even if the cell is
            # empty — use "" rather than omitting the key. This keeps
            # every record's key set identical, which matters for
            # loading the output into Excel/a database with consistent
            # columns (a row with a blank "rig" cell still gets
            # "rig": "" instead of dropping the key entirely).
            record = {key: "" for key in headers}
            raw_record = {key: "" for key in headers}
            has_any_value = False
            for key, value, raw_value in zip(headers, row_cells, raw_row_cells):
                if value:
                    record[key] = value
                    raw_record[key] = raw_value
                    has_any_value = True

            # Skip rows where every cell is empty (e.g. a genuinely blank
            # table row, or trailing whitespace Camelot picked up as a
            # phantom row) — only this all-empty case is dropped; every
            # row that's kept still has every header as a key.
            if has_any_value:
                results.append(record)
                raw_results.append(raw_record)

        return results, headers, raw_results

    return [], known_headers, []


def extract_table_for_page(pdf_path, page_number):
    """
    Tries Camelot on a single page. Returns a list of row strings (one
    per table row) if a real table was found, otherwise an empty list.
    Kept for the plain-line JSON output mode (rows like "A | B | C").
    """
    rows = []
    try:
        tables = camelot.read_pdf(pdf_path, pages=str(page_number), flavor="lattice")
        if tables.n == 0:
            tables = camelot.read_pdf(pdf_path, pages=str(page_number), flavor="stream")

        for table in tables:
            df = table.df
            if not looks_like_real_table(df):
                continue
            for _, table_row in df.iterrows():
                row_text = " | ".join(str(cell).strip() for cell in table_row if str(cell).strip())
                if row_text:
                    rows.append(row_text)
    except Exception as e:
        # Camelot can throw errors if Ghostscript isn't installed, or the
        # page has no tables. We simply treat that as "no table found".
        print(f"[Camelot] Skipped on page {page_number}: {e}")
        rows = []

    return rows


# ---------------------------------------------------------------------------
# Extraction Step 2: per-page hybrid text / OCR pipeline
# ---------------------------------------------------------------------------
def extract_text_for_page(pdf, page_index):
    """
    Extracts real text from a single page using pdfplumber, line by line.
    `pdf` is an already-open pdfplumber.PDF object.
    """
    lines = []
    page = pdf.pages[page_index]
    text = page.extract_text()
    if text:
        for line in text.split("\n"):
            line = line.strip()
            if line:
                lines.append(line)
    return lines


def extract_ocr_for_page(pdf_path, page_number):
    """
    OCRs a single page (1-indexed page_number) by rendering it to an
    image with pdf2image and running Tesseract on it. Only called for
    pages classified as "image" (no real text layer).
    """
    lines = []
    if not OCR_AVAILABLE:
        return lines

    try:
        # first_page/last_page let us render just the one page we need,
        # instead of rasterizing the whole document for every page.
        page_images = convert_from_path(pdf_path, first_page=page_number, last_page=page_number)
        for page_image in page_images:
            text = pytesseract.image_to_string(page_image)
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    lines.append(line)
    except Exception as e:
        print(f"[OCR] Failed on page {page_number}: {e}")

    return lines


def extract_pdf_data_per_page(pdf_path):
    """
    Classifies every page, then extracts each page with the correct
    method for ITS OWN content (not a single whole-document guess):

      - "text"  page -> try Camelot for a real table first; if no real
                        table is found on that page, fall back to plain
                        pdfplumber text extraction
      - "mixed" page -> same as "text" (a real text layer is present,
                        so OCR is unnecessary even though an image also
                        sits on the page)
      - "image" page -> OCR (if available) — Camelot/pdfplumber text
                        extraction cannot read image-only pages at all
      - "empty" page -> nothing to extract

    For table pages, BOTH representations are collected:
      - a flat "row" string ("A | B | C"), for the simple/plain JSON view
      - a structured dict ({"id": "A", "date": "B", ...}), built from
        the header row found on the first page of the table. If a later
        page continues the SAME table (same column count, no new header
        row printed), the same headers are reused automatically — this
        is what correctly handles multi-page tables like long reports
        where the header only appears once.

    Returns:
      rows                   -> [{"row": N, "page": P, "data": "..."}]
      structured_records     -> [{"id": "...", "date": "...", ...}, ...],
                                 whitespace-cleaned
      raw_structured_records -> same shape, NOT whitespace-cleaned (used
                                 by resolve_constant_column_leak() to
                                 detect/undo a merged-cell leak)
      page_summary           -> [{"page": P, "type": ..., "extraction_method": ...,
                              "ocr_used": bool, "lines_extracted": N}]
    """
    classifications = classify_pages(pdf_path)

    rows = []
    structured_records = []
    raw_structured_records = []
    page_summary = []
    row_number = 1
    carried_headers = None  # headers from the previous table page, if any

    with pdfplumber.open(pdf_path) as pdf:
        for entry in classifications:
            page_num = entry["page"]
            has_text = entry["has_text"]
            has_image = entry["has_image"]
            image_count = entry["image_count"]
            page_index = page_num - 1
            label = page_type_label(has_text, has_image)

            extraction_method = "none"
            ocr_used = False
            page_lines = []

            if label in ("text", "mixed"):
                # Real text layer exists. Try Camelot first in case this
                # page contains an actual table (richer structure than
                # plain lines); only accept it if looks_like_real_table()
                # confirms it, otherwise use plain text extraction.
                structured_rows, headers_used, raw_rows_for_page = extract_structured_table_for_page(
                    pdf_path, page_num, known_headers=carried_headers
                )

                if structured_rows:
                    carried_headers = headers_used
                    structured_records.extend(structured_rows)
                    raw_structured_records.extend(raw_rows_for_page)
                    # Keep the flat string view in sync too, so both
                    # output formats describe the same data.
                    page_lines = [
                        " | ".join(str(v) for v in record.values())
                        for record in structured_rows
                    ]
                    extraction_method = "camelot_table"
                else:
                    # No real table on this page -> this page is not part
                    # of a table sequence, so stop carrying headers forward.
                    carried_headers = None
                    page_lines = extract_text_for_page(pdf, page_index)
                    extraction_method = "text_layer"

            elif label == "image":
                carried_headers = None
                # No real text layer -> only option is OCR, if installed.
                if OCR_AVAILABLE:
                    page_lines = extract_ocr_for_page(pdf_path, page_num)
                    extraction_method = "ocr"
                    ocr_used = True
                else:
                    extraction_method = "ocr_unavailable"

            else:  # "empty"
                carried_headers = None
                extraction_method = "none"

            for line in page_lines:
                rows.append({"row": row_number, "page": page_num, "data": line})
                row_number += 1

            page_summary.append({
                "page": page_num,
                "type": label,
                "has_text": has_text,
                "has_image": has_image,
                "image_count": image_count,
                "extraction_method": extraction_method,
                "ocr_used": ocr_used,
                "lines_extracted": len(page_lines),
            })

    return rows, structured_records, raw_structured_records, page_summary


# ---------------------------------------------------------------------------
# Post-processing: fix a narrow column whose tall, visually-merged cell
# Camelot reads as one cell spanning several short table rows
# ---------------------------------------------------------------------------
LEAK_DETECTION_MIN_RECORDS = 5     # don't try to detect a pattern on tiny tables
LEAK_DETECTION_MIN_RATIO = 0.25    # the constant word must lead in a meaningful
                                    # share of records — NOT "almost all" of them.
                                    # The leak itself means many rows show this
                                    # word in neither column (it's missing from
                                    # its home cell AND from the neighbor it
                                    # would otherwise prefix), so a high bar like
                                    # 0.85 misses real cases. What actually marks
                                    # this pattern is being a clear OUTLIER above
                                    # every other candidate, checked below.
LEAK_DETECTION_MIN_OUTLIER_RATIO = 3.0  # dominant word's count must be at least
                                         # this many times the next-best candidate's


def resolve_constant_column_leak(structured_records, raw_structured_records, headers):
    """
    Fixes a real, confirmed extraction problem: when a table column's
    value is the SAME short constant for many rows in a row (e.g. a rig
    code like "TNG"), the PDF often renders it as a single tall cell
    visually spanning several short rows, rather than repeating the
    value in every row's own cell. Camelot's lattice mode reads the
    PDF's underlying grid geometry, so it attaches that WHOLE merged
    cell — including all of its visual lines — to only the FIRST row
    in the span. Every other row in that span ends up with this
    column's cell empty, AND loses the first line of its own next-door
    column's text (e.g. "whatWasSeen"), because that line was physically
    inside the merged cell's bounding box, not in the row's own cell.

    Concretely, the first row's "rig" cell ends up holding multiple
    newline-joined fragments, each shaped like "<dominant_word> <text>"
    — one fragment per row in the span, in top-to-bottom order — while
    each of those other rows shows an empty or truncated "whatWasSeen".

    This function:
      1. Detects the dominant constant word the same way as before: a
         short leading word that appears as a clean prefix inside the
         value of MORE THAN ONE different key across the table (proof
         it's crossing column boundaries — a genuine column like "date"
         or "id" never does this), accounting for nearly every record.
      2. Identifies which header is this constant's "home" column
         (whichever header most often holds EXACTLY this word alone).
      3. For every record whose home-column cell holds more than just
         the bare constant, splits that RAW (newline-preserving) cell
         into its fragments and redistributes them — IN ORDER — onto
         the whatWasSeen-equivalent column of the next rows whose own
         value doesn't already start with the constant. This must use
         raw_structured_records, not the whitespace-cleaned records,
         because the newlines are the only reliable boundary between
         one row's fragment and the next; once collapsed into spaces
         (as clean_cell_text() does for the normal output) the
         fragments can no longer be told apart.

    Returns (fixed_records, fix_count, warnings):
      fixed_records -> same shape as structured_records, repaired
      fix_count     -> how many records were changed (0 if nothing
                        needed fixing — e.g. a PDF without this layout)
      warnings      -> list of human-readable strings flagging any row
                        where the number of fragments didn't exactly
                        match the number of candidate rows. These rows
                        are left untouched rather than guessed at, so
                        nothing is silently mis-assigned.
    """
    if not structured_records or not headers or len(structured_records) < LEAK_DETECTION_MIN_RECORDS:
        return structured_records, 0, []

    # --- Step 1: detect the dominant constant word (unchanged from the
    # original heuristic — this part was always correct). ---
    word_to_keys = {}
    word_to_count = Counter()
    for record in structured_records:
        for key in headers:
            value = record.get(key, "")
            if not value:
                continue
            leading_word = value.split(" ", 1)[0]
            if len(leading_word) > 12:
                continue  # too long to be a code/tag like "TNG"
            word_to_keys.setdefault(leading_word, set()).add(key)
            word_to_count[leading_word] += 1

    candidates = [
        word for word, keys in word_to_keys.items()
        if len(keys) >= 2 and (word_to_count[word] / len(structured_records)) >= LEAK_DETECTION_MIN_RATIO
    ]

    if not candidates:
        return structured_records, 0, []

    dominant_word = max(candidates, key=lambda w: word_to_count[w])

    # Require the dominant word to be a clear outlier above every other
    # candidate — a real leaked constant (like "TNG") vastly out-leads
    # any other word that happens to start two different fields (e.g.
    # "I" or "God" starting both whatWasDiscussed and whatWasReinforced
    # by sheer coincidence in a handful of rows). This guards against
    # false positives without requiring an unrealistically high absolute
    # ratio, since the leak itself suppresses the count for affected rows.
    other_candidates = [w for w in candidates if w != dominant_word]
    if other_candidates:
        next_best = max(word_to_count[w] for w in other_candidates)
        if word_to_count[dominant_word] < LEAK_DETECTION_MIN_OUTLIER_RATIO * next_best:
            return structured_records, 0, []

    # --- Step 2: find the constant's home column (whichever header most
    # often holds EXACTLY this word alone). ---
    exact_match_counts = Counter()
    for record in structured_records:
        for key in headers:
            if record.get(key, "").strip() == dominant_word:
                exact_match_counts[key] += 1

    if exact_match_counts:
        home_key = exact_match_counts.most_common(1)[0][0]
    else:
        home_key = "rig" if "rig" in headers else f"{dominant_word.lower()}_column"

    # The neighboring column that actually absorbs the leaked text is
    # whichever column most often immediately follows home_key in the
    # header order — for this app that's always the next header listed.
    if home_key in headers:
        home_index = headers.index(home_key)
        neighbor_key = headers[home_index + 1] if home_index + 1 < len(headers) else None
    else:
        neighbor_key = None

    if neighbor_key is None:
        return structured_records, 0, []

    prefix_pattern = re.compile(r"^" + re.escape(dominant_word) + r"(\s+|$)")

    fixed_records = [dict(r) for r in structured_records]
    fixed_count = 0
    warnings = []

    i = 0
    n = len(fixed_records)
    while i < n:
        raw_home_value = raw_structured_records[i].get(home_key, "") if i < len(raw_structured_records) else ""
        home_clean = " ".join(raw_home_value.split()).strip()

        if home_clean == dominant_word or home_clean == "":
            i += 1
            continue

        # This row's home-column cell holds more than the bare constant —
        # it's a leaked merged cell. Split it into fragments: by newline
        # if the cell spans multiple rows, otherwise the whole (cleaned)
        # cell is itself the single fragment (a 2-row-tall merge often
        # has no internal newline at all).
        if "\n" in raw_home_value:
            segments = [s.strip() for s in raw_home_value.split("\n") if s.strip()]
        else:
            segments = [home_clean] if home_clean else []

        dominant_segments = [s for s in segments if prefix_pattern.match(s)]

        if not dominant_segments:
            # Doesn't match the expected leak pattern — leave this row
            # untouched rather than guessing, and flag it for review.
            warnings.append(
                f"Row {i} (id={fixed_records[i].get('id', '?')}): "
                f"unrecognized {home_key!r} content {home_clean!r}, left as-is."
            )
            i += 1
            continue

        # Candidate rows: starting at this row, every row whose OWN
        # neighbor-column value doesn't already start with the constant
        # — those are the rows that lost their fragment to the merge,
        # in the same top-to-bottom order the merged cell's lines appear.
        candidate_indices = []
        j = i
        while j < n and len(candidate_indices) < len(dominant_segments):
            raw_neighbor_value = raw_structured_records[j].get(neighbor_key, "") if j < len(raw_structured_records) else ""
            neighbor_clean = " ".join(raw_neighbor_value.split()).strip()
            if not prefix_pattern.match(neighbor_clean):
                candidate_indices.append(j)
            j += 1

        if len(candidate_indices) != len(dominant_segments):
            warnings.append(
                f"Row {i} (id={fixed_records[i].get('id', '?')}): "
                f"found {len(dominant_segments)} merged fragment(s) but "
                f"{len(candidate_indices)} candidate row(s) needing one — "
                f"left these rows untouched rather than risk a wrong match."
            )
            i += 1
            continue

        for segment, target_idx in zip(dominant_segments, candidate_indices):
            remainder = prefix_pattern.sub("", segment, count=1).strip()
            existing = fixed_records[target_idx].get(neighbor_key, "").strip()
            fixed_records[target_idx][neighbor_key] = (
                remainder + (" " + existing if existing else "")
            ).strip()
            fixed_records[target_idx][home_key] = dominant_word
            fixed_count += 1

        if fixed_records[i].get(home_key, "") != dominant_word:
            fixed_records[i][home_key] = dominant_word
            fixed_count += 1

        i += 1

    # Final cleanup pass across every record: strip a redundant leading
    # "<dominant_word> " from the neighbor column for rows that already
    # had their own clean, unmerged text (e.g. "TNG Ryddig rundt BOP" ->
    # "Ryddig rundt BOP"), and make sure every home_key reads as the
    # clean constant rather than being left blank.
    for record in fixed_records:
        neighbor_value = record.get(neighbor_key, "")
        match = prefix_pattern.match(neighbor_value)
        if match:
            record[neighbor_key] = neighbor_value[match.end():].strip()
        if record.get(home_key, "").strip() == "":
            record[home_key] = dominant_word

    return fixed_records, fixed_count, warnings


# ---------------------------------------------------------------------------
# Master function: classification-driven extraction pipeline
# ---------------------------------------------------------------------------
def extract_pdf_data(pdf_path):
    """
    Single entry point. Every page is classified first (text / image /
    mixed / empty), then extracted with the method that actually fits
    that page's content — this is what makes the result accurate for
    documents that mix scanned pages, plain text pages, tables, and
    pages with both text and pictures together.

    After per-page extraction, a post-processing pass checks for a
    narrow constant-valued column whose tall, visually-merged PDF cell
    got attached to the wrong row by Camelot (see
    resolve_constant_column_leak) and fixes it if detected.

    Returns (rows, structured_records, page_summary, method_used)
    """
    print("Classifying every page (text / image / mixed / empty)...")
    rows, structured_records, raw_structured_records, page_summary = extract_pdf_data_per_page(pdf_path)

    if structured_records:
        # Preserve the ORDER headers first appeared in (not alphabetical) —
        # resolve_constant_column_leak relies on column order to find which
        # header immediately follows the constant's "home" column.
        all_headers = list(structured_records[0].keys())
        structured_records, fix_count, warnings = resolve_constant_column_leak(
            structured_records, raw_structured_records, all_headers
        )
        if fix_count:
            print(f"Restored a leaked merged-cell value in {fix_count} record(s).")
        for warning in warnings:
            print(f"[Leak fix] {warning}")

    print(f"Done. {len(page_summary)} page(s) classified, {len(rows)} row(s) extracted, "
          f"{len(structured_records)} structured record(s) built.")
    return rows, structured_records, page_summary, "per_page_classification"


# ---------------------------------------------------------------------------
# Shared core: run the pipeline on a saved PDF path and write the two
# output JSON files. Used by every route (file upload, URL fetch, the
# JSON API) so they all behave identically and stay in sync.
# ---------------------------------------------------------------------------
def run_pipeline_and_save(pdf_path, source_label):
    """
    Runs extract_pdf_data() on an already-saved PDF, writes the two
    output JSON files (full detail + records-only), and returns the
    full response payload as a dict, plus timing info.

    `source_label` is what gets recorded as "source_file" in the output
    — the original filename for uploads, or the original URL for
    URL-based requests (so the output stays traceable to where it came
    from even though the file on disk has a generated name).
    """
    started_at = time.monotonic()

    extracted_rows, structured_records, page_summary, method_used = extract_pdf_data(pdf_path)

    elapsed_seconds = round(time.monotonic() - started_at, 3)

    type_counts = {"text": 0, "image": 0, "mixed": 0, "empty": 0}
    for p in page_summary:
        type_counts[p["type"]] = type_counts.get(p["type"], 0) + 1

    # Output filenames are derived from the on-disk PDF name (which is
    # always safe/unique), not from source_label (which may be a URL
    # full of characters that aren't safe in a filename).
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    json_filename = f"{base_name}.json"
    json_path = os.path.join(OUTPUT_FOLDER, json_filename)

    records_filename = None
    if structured_records:
        records_filename = f"{base_name}.records.json"
        records_path = os.path.join(OUTPUT_FOLDER, records_filename)
        with open(records_path, "w", encoding="utf-8") as f:
            json.dump(structured_records, f, indent=2, ensure_ascii=False)

    # The SAVED/DOWNLOADED JSON file intentionally contains ONLY the
    # extracted records -- no metadata (source_file, page_count,
    # page_type_counts, extraction_method_used, ocr_available,
    # processing_seconds, the per-page "pages" breakdown, or the
    # redundant pipe-joined "data" rows). Those fields are all still
    # useful for the LIVE HTTP response (response_payload below), which
    # is what the website actually reads to render the page-count
    # summary, the per-page table, and the status message right after
    # upload -- removing them there would break that UI. But once
    # someone downloads the .json file to actually use the data, all
    # they want is the clean, structured records themselves.
    #
    # If structured_records is empty (e.g. no table was found on any
    # page), fall back to the raw extracted_rows so the downloaded file
    # still contains the actual extracted content rather than an empty
    # array -- this matches the same fallback the UI itself already
    # uses (see previewSource in script.js).
    output_payload = structured_records if structured_records else extracted_rows

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2, ensure_ascii=False)

    response_payload = {
        "message": "PDF processed successfully.",
        "json_filename": json_filename,
        "records_filename": records_filename,
        "row_count": len(extracted_rows),
        "record_count": len(structured_records),
        "page_count": len(page_summary),
        "page_type_counts": type_counts,
        "processing_seconds": elapsed_seconds,
        "pages_per_second": round(len(page_summary) / elapsed_seconds, 3) if elapsed_seconds > 0 else None,
        "pages": page_summary,
        "records": structured_records,
        "data": extracted_rows,
    }
    return response_payload


def process_one_pdf_for_batch(pdf_path, source_label, original_filename):
    """
    The function actually executed inside a worker process from
    pdf_worker_pool. Must be a top-level function (not a closure or
    method) so it can be pickled and sent to the worker process.

    Each worker process that runs this imports its OWN copies of
    camelot/pdfplumber/etc the first time this module loads in that
    process -- that's the one-time ~150-200MB cost discussed above
    pdf_worker_pool's creation. Subsequent calls to this same already-
    running worker reuse those already-imported libraries for free.

    Returns a plain dict (must be picklable to send back to the main
    process) describing either success or failure for this one file --
    never raises, so one bad PDF in a batch can't crash the whole batch
    or leave other futures in an unclear state.
    """
    try:
        response_payload = run_pipeline_and_save(pdf_path, source_label)
        response_payload["original_filename"] = original_filename
        response_payload["status"] = "success"
        return response_payload
    except Exception as e:
        return {
            "status": "error",
            "original_filename": original_filename,
            "error": f"Processing failed: {e}",
        }
    finally:
        # Each batch upload's source PDF is only needed for this one
        # processing run -- once extraction is done (or has failed),
        # the uploaded copy on disk is no longer needed. (The output
        # JSON files written by run_pipeline_and_save are kept, same as
        # the existing single-file /upload behavior, so downstream
        # download links keep working.)
        delete_file_quietly(pdf_path)


# Path to the frontend folder, so Flask can serve it directly. This
# means the SAME Flask server (and therefore the SAME public URL once
# deployed) can serve both the API and the upload page — no separate
# frontend hosting needed, and no risk of the frontend pointing at the
# wrong backend address after deployment.
FRONTEND_FOLDER = os.path.join(BASE_DIR, "..", "frontend")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    """
    Serves the upload page itself, so visiting the deployed URL in a
    browser shows a usable interface rather than a bare JSON blob.
    Falls back to a JSON status message if the frontend folder isn't
    present for some reason (e.g. a deployment that only ships backend/).
    """
    index_path = os.path.join(FRONTEND_FOLDER, "index.html")
    if os.path.exists(index_path):
        return send_from_directory(FRONTEND_FOLDER, "index.html")
    return jsonify({
        "message": "PDF to JSON converter backend is running. (No frontend/ folder found to serve.)",
        "see": "/api",
    })


@app.route("/<path:filename>")
def frontend_assets(filename):
    """
    Serves the frontend's other static files (script.js, style.css) at
    the same path the HTML expects them. Only matches files that exist
    in FRONTEND_FOLDER; anything else falls through to Flask's normal
    404, so this doesn't swallow API routes registered elsewhere.
    """
    file_path = os.path.join(FRONTEND_FOLDER, filename)
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return send_from_directory(FRONTEND_FOLDER, filename)
    return jsonify({"error": "Not found."}), 404


@app.route("/api")
def api_status():
    """
    Machine-readable status + endpoint list. This used to live at "/",
    but "/" now serves the frontend page instead — use this route for
    a quick health/capability check from a script or curl.
    """
    return jsonify({
        "message": "PDF to JSON converter backend is running.",
        "pymupdf_available": PYMUPDF_AVAILABLE,
        "ocr_available": OCR_AVAILABLE,
        "concurrency_model": "ram_budget_per_pdf",
        # Exposed so external callers (the benchmark script, or any other
        # client) can discover the server's REAL per-request file-count
        # cap and respect it automatically, instead of guessing a number
        # and finding out it's wrong only when a request gets a 400.
        # This was previously invisible from outside the Flask process --
        # a client had no way to know this value without hardcoding a
        # guess that could silently drift out of sync with the server's
        # actual configured limit.
        "max_files_per_batch": MAX_FILES_PER_BATCH,
        # Same reasoning as max_files_per_batch above -- the TOTAL
        # request-size cap (Flask's MAX_CONTENT_LENGTH, fed from
        # MAX_PDF_BYTES) was previously invisible from outside the
        # process too. Exposed here so a client can check, before
        # uploading, whether its planned request will fit, instead of
        # discovering the limit only via an HTTP 413.
        "max_total_request_bytes": MAX_PDF_BYTES,
        "max_total_request_mb": round(MAX_PDF_BYTES / (1024 * 1024), 1),
        "pool_status": pdf_worker_pool.status(),
        "endpoints": {
            "POST /upload": "Form-data upload — accepts a 'file' field (PDF) OR a 'url' field (direct PDF link). Used by the web frontend. Output files are kept temporarily so /download works, then auto-deleted after a few hours.",
            "POST /upload-batch": "Form-data upload for MULTIPLE files at once — repeated 'files' fields. Each file's RAM cost is estimated individually; as many run concurrently as currently-available RAM allows, not a fixed worker count. Extras queue automatically and are admitted as budget frees up. Capped at max_files_per_batch files per single request — use /upload-batch-job if you have more than that.",
            "POST /upload-batch-job": "Like /upload-batch, but accepts MORE files than max_files_per_batch in one request — the server splits them into weight-balanced internal sub-batches automatically, so the caller doesn't need to. Still subject to the total request-size cap (MAX_PDF_BYTES).",
            "POST /api/process": "JSON API — accepts {\"url\": \"...\"} body, or a 'file' multipart field. Built for programmatic/automated callers. Output files are deleted immediately after the response is sent, since the data is already included in the response body.",
            "GET /download/<filename>": "Download a previously generated JSON output file.",
            "GET /health": "Health check for container orchestration / uptime monitors.",
        },
    })


@app.route("/health")
def health():
    """
    Lightweight health check for Docker/Railway/uptime monitors. Doesn't
    touch the filesystem or run any PDF libraries, so it stays fast and
    reliable even while a large PDF is mid-processing on another request.
    """
    return jsonify({
        "status": "ok",
        "pymupdf_available": PYMUPDF_AVAILABLE,
        "ocr_available": OCR_AVAILABLE,
    })


@app.route("/upload", methods=["POST"])
def upload_pdf():
    """
    Accepts EITHER:
      - a "file" field in multipart/form-data (a PDF upload), OR
      - a "url" field (form field or JSON body) pointing directly at a
        PDF file, which the server downloads itself.

    Classifies every page (text / image / mixed / empty), runs the
    extraction pipeline, saves TWO JSON files:

      - <name>.json          -> full detail: per-page breakdown + flat
                                 "row" view (useful for debugging/auditing)
      - <name>.records.json  -> just the clean array of structured records
                                 (e.g. [{"id": ..., "date": ..., ...}, ...]),
                                 only produced when a real table was found —
                                 this is the ready-to-use data file

    Returns both file names + all the data to the caller.
    """
    pdf_path = None
    source_label = None
    downloaded = False

    if "file" in request.files and request.files["file"].filename != "":
        uploaded_file = request.files["file"]
        if not uploaded_file.filename.lower().endswith(ALLOWED_EXTENSION):
            return jsonify({"error": "Only PDF files are allowed."}), 400
        pdf_filename = uploaded_file.filename
        pdf_path = safe_unique_upload_path(pdf_filename)
        uploaded_file.save(pdf_path)
        source_label = pdf_filename
    else:
        if request.is_json:
            url = (request.get_json(silent=True) or {}).get("url")
        else:
            url = request.form.get("url")
        if not url:
            return jsonify({"error": "No file part in the request, and no 'url' field provided."}), 400
        try:
            pdf_path = download_pdf_from_url(url)
        except PdfFetchError as e:
            return jsonify({"error": str(e)}), 400
        source_label = url
        downloaded = True

    try:
        response_payload = run_pipeline_and_save(pdf_path, source_label)
    except Exception as e:
        return jsonify({"error": f"Processing failed: {e}"}), 500
    finally:
        # Downloaded temp files are cleaned up immediately after processing
        # (they were only fetched to be processed, no reason to keep
        # them); uploaded files are kept in uploads/ for a while so the
        # "Download JSON" button still works, but get swept by the
        # background cleanup job after FILE_RETENTION_HOURS.
        if downloaded:
            delete_file_quietly(pdf_path)

    return jsonify(response_payload)


def _process_saved_files_through_pool(saved):
    """
    Shared core logic: takes a list of (pdf_path, original_filename)
    tuples whose files are ALREADY saved to disk, submits every one to
    the RAM-budget pool, waits for all of them, and returns the list of
    per-file result dicts in the SAME ORDER as the input list.

    Extracted out of upload_batch() so /upload-batch-job (which may
    call this once per internal sub-batch, for files beyond
    MAX_FILES_PER_BATCH) can reuse the exact same submission/collection
    logic instead of a second, slightly-different copy of it.
    """
    future_to_path = {
        pdf_worker_pool.submit(process_one_pdf_for_batch, pdf_path, original_filename, original_filename): pdf_path
        for pdf_path, original_filename in saved
    }

    results_by_path = {}
    for pending in as_completed_ram_budget(list(future_to_path.keys())):
        pdf_path = future_to_path[pending]
        result = pending.result()  # process_one_pdf_for_batch never raises -- always returns a dict
        results_by_path[pdf_path] = result

    return [results_by_path[pdf_path] for pdf_path, _ in saved]


@app.route("/upload-batch", methods=["POST"])
def upload_batch():
    """
    Accepts MULTIPLE PDF files in one request (multipart/form-data,
    repeated "files" fields) and processes them using pdf_worker_pool
    (RamBudgetPool), which estimates each file's own RAM cost up front
    and admits as many of them to run genuinely simultaneously, in
    separate worker processes, as the CURRENT real RAM budget allows --
    not a fixed number decided once at startup. A batch of small PDFs
    can run more of them concurrently than a batch of large/scanned
    ones; the concurrency adapts per-file (see pdf_cost_estimator.py
    and ram_budget_pool.py).

    Every file gets saved to disk under a unique generated name FIRST
    (so two files named "report.pdf" from the same batch, or from two
    different simultaneous requests, can never collide -- see
    safe_unique_upload_path), and only THEN is the batch of saved paths
    handed to the worker pool.

    Returns once every file in the batch has either finished processing
    or failed -- the response is a JSON array, one entry per input file,
    in the SAME ORDER the files were sent, each shaped like a single
    /upload response (on success) plus "original_filename" and
    "status", or {"status": "error", "original_filename": ..., "error":
    ...} for any file that failed. One bad/corrupt PDF in the batch
    cannot prevent the other files in the same batch from completing
    and being returned successfully.

    HARD CAP: this endpoint accepts at most MAX_FILES_PER_BATCH files
    in ONE request (rejects with HTTP 400 if exceeded). If you have
    MORE files than that and want the server to split them into
    multiple internal sub-batches automatically, use
    /upload-batch-job instead.
    """
    if "files" not in request.files:
        return jsonify({"error": "No 'files' field in the request. Send one or more files under the 'files' field name."}), 400

    uploaded_files = [f for f in request.files.getlist("files") if f.filename]
    if not uploaded_files:
        return jsonify({"error": "No files were selected."}), 400

    if len(uploaded_files) > MAX_FILES_PER_BATCH:
        return jsonify({
            "error": f"Too many files in one batch ({len(uploaded_files)}). "
                     f"The limit is {MAX_FILES_PER_BATCH} files per request — "
                     f"please split this into smaller batches, or use "
                     f"/upload-batch-job, which does that splitting for you "
                     f"automatically.",
        }), 400

    # Validate every file's extension UP FRONT, before saving or
    # processing anything -- if even one file in the batch isn't a PDF,
    # reject the whole batch immediately with a clear error, rather
    # than burning worker-pool time on the valid files first and
    # confusingly failing partway through.
    for f in uploaded_files:
        if not f.filename.lower().endswith(ALLOWED_EXTENSION):
            return jsonify({
                "error": f"Only PDF files are allowed. '{f.filename}' is not a PDF.",
            }), 400

    # Save every file to its own unique on-disk path BEFORE submitting
    # any work to the pool. Saving must happen here, on the main
    # request-handling process, because Flask's uploaded_file objects
    # are tied to this request's context and can't be safely sent
    # across to a worker process.
    #
    # IMPORTANT: results are tracked by pdf_path (guaranteed unique by
    # safe_unique_upload_path), NOT by original_filename. Two files in
    # the same batch can legitimately share an original_filename (e.g.
    # someone selecting "report.pdf" from two different folders) -- an
    # earlier version of this code keyed results by original_filename
    # alone, which silently collapsed same-named entries down to just
    # one result, duplicated across every slot that shared that name.
    # Caught by testing this exact case against a real batch, not
    # assumed safe.
    saved = []  # list of (pdf_path, original_filename) in input order
    for f in uploaded_files:
        pdf_path = safe_unique_upload_path(f.filename)
        f.save(pdf_path)
        saved.append((pdf_path, f.filename))

    ordered_results = _process_saved_files_through_pool(saved)

    success_count = sum(1 for r in ordered_results if r.get("status") == "success")
    return jsonify({
        "message": f"Processed {len(ordered_results)} file(s): {success_count} succeeded, {len(ordered_results) - success_count} failed.",
        "file_count": len(ordered_results),
        "success_count": success_count,
        "error_count": len(ordered_results) - success_count,
        "results": ordered_results,
    })


@app.route("/upload-batch-job", methods=["POST"])
def upload_batch_job():
    """
    Like /upload-batch, but accepts MORE files than MAX_FILES_PER_BATCH
    in a single request, and the SERVER splits them into multiple
    internal sub-batches automatically -- the caller doesn't need to
    do that splitting themselves, or send multiple separate requests.

    WHY THIS EXISTS (separately from /upload-batch):
    /upload-batch enforces a hard per-request file-count cap
    (MAX_FILES_PER_BATCH) and rejects anything over it outright -- by
    design, since accepting an unbounded single request risks one
    request consuming all available memory/disk on a small server. But
    that meant any caller with MORE files than the cap had to implement
    their own splitting logic to use this service at all (this is
    exactly what benchmark.py's balance_into_batches() does, on the
    CLIENT side). Most real callers (a website's upload form, another
    backend service) shouldn't have to re-implement that logic
    themselves just to upload, say, 100 files. This endpoint does the
    same splitting SERVER-SIDE, using the identical weight-balancing
    algorithm (batch_balancer.balance_into_batches -- the same function
    benchmark.py now imports, so the two can never drift apart) so no
    single internal sub-batch ends up with all the heavy PDFs while
    another gets all the light ones.

    NOTE ON TOTAL REQUEST SIZE: this does NOT relax MAX_PDF_BYTES (the
    total-size cap Flask enforces via MAX_CONTENT_LENGTH before any of
    this code even runs). A request with more files than
    MAX_FILES_PER_BATCH must still fit under MAX_PDF_BYTES in total
    bytes, or Flask will reject it with HTTP 413 before this endpoint's
    code executes at all -- raise MAX_PDF_BYTES (env var) if you need
    to send a larger TOTAL payload across many files.

    Returns once every sub-batch has finished. The response shape
    matches /upload-batch (file_count/success_count/error_count/
    results, in original input order), plus a "sub_batches" field
    describing how the files were internally grouped, so callers can
    see the same kind of breakdown the benchmark script's reports show.
    """
    if "files" not in request.files:
        return jsonify({"error": "No 'files' field in the request. Send one or more files under the 'files' field name."}), 400

    uploaded_files = [f for f in request.files.getlist("files") if f.filename]
    if not uploaded_files:
        return jsonify({"error": "No files were selected."}), 400

    for f in uploaded_files:
        if not f.filename.lower().endswith(ALLOWED_EXTENSION):
            return jsonify({
                "error": f"Only PDF files are allowed. '{f.filename}' is not a PDF.",
            }), 400

    # Save every file first (same reasoning as /upload-batch: must
    # happen on the main request-handling process).
    saved = []  # list of (pdf_path, original_filename), in input order
    path_to_original = {}
    for f in uploaded_files:
        pdf_path = safe_unique_upload_path(f.filename)
        f.save(pdf_path)
        saved.append((pdf_path, f.filename))
        path_to_original[pdf_path] = f.filename

    # Split into weight-balanced sub-batches, each respecting
    # MAX_FILES_PER_BATCH, using the SAME algorithm and the SAME
    # per-file cost estimator the benchmark script and the RAM-budget
    # pool itself rely on -- see batch_balancer.py.
    all_paths = [p for p, _ in saved]
    sub_batches, sub_batch_weights = balance_into_batches(all_paths, MAX_FILES_PER_BATCH)

    results_by_path = {}
    sub_batch_summaries = []
    for index, (sub_batch_paths, weight) in enumerate(zip(sub_batches, sub_batch_weights), start=1):
        sub_batch_saved = [(p, path_to_original[p]) for p in sub_batch_paths]
        sub_batch_results = _process_saved_files_through_pool(sub_batch_saved)
        for (p, _), result in zip(sub_batch_saved, sub_batch_results):
            results_by_path[p] = result
        sub_batch_summaries.append({
            "sub_batch_index": index,
            "file_count": len(sub_batch_paths),
            "estimated_weight_mb": round(weight, 1),
        })

    ordered_results = [results_by_path[p] for p, _ in saved]
    success_count = sum(1 for r in ordered_results if r.get("status") == "success")

    return jsonify({
        "message": f"Processed {len(ordered_results)} file(s) across {len(sub_batches)} internal "
                    f"sub-batch(es): {success_count} succeeded, {len(ordered_results) - success_count} failed.",
        "file_count": len(ordered_results),
        "success_count": success_count,
        "error_count": len(ordered_results) - success_count,
        "sub_batches": sub_batch_summaries,
        "results": ordered_results,
    })


@app.route("/api/process", methods=["POST"])
def api_process():
    """
    JSON-first endpoint intended for other tools/services to call
    automatically (no human clicking "upload"). Two ways to call it:

      1. JSON body:      POST /api/process
                          Content-Type: application/json
                          {"url": "https://example.com/file.pdf"}

      2. Multipart file: POST /api/process
                          Content-Type: multipart/form-data
                          file=<the PDF>

    Response shape is identical to /upload, plus a "request_id" so
    callers can correlate logs, and always includes "processing_seconds"
    for throughput measurement.
    """
    request_id = uuid.uuid4().hex[:12]
    pdf_path = None
    source_label = None
    downloaded = False

    if "file" in request.files and request.files["file"].filename != "":
        uploaded_file = request.files["file"]
        if not uploaded_file.filename.lower().endswith(ALLOWED_EXTENSION):
            return jsonify({"request_id": request_id, "error": "Only PDF files are allowed."}), 400
        pdf_filename = uploaded_file.filename
        pdf_path = safe_unique_upload_path(pdf_filename)
        uploaded_file.save(pdf_path)
        source_label = pdf_filename
    else:
        body = request.get_json(silent=True) or {}
        url = body.get("url")
        if not url:
            return jsonify({
                "request_id": request_id,
                "error": "Provide either a multipart 'file' field or a JSON body with a 'url' field.",
            }), 400
        try:
            pdf_path = download_pdf_from_url(url)
        except PdfFetchError as e:
            return jsonify({"request_id": request_id, "error": str(e)}), 400
        source_label = url
        downloaded = True

    try:
        response_payload = run_pipeline_and_save(pdf_path, source_label)
    except Exception as e:
        return jsonify({"request_id": request_id, "error": f"Processing failed: {e}"}), 500
    finally:
        if downloaded:
            delete_file_quietly(pdf_path)

    # This endpoint already returns the full extracted data in the
    # response body above, so the on-disk JSON files it created (via
    # run_pipeline_and_save) are redundant the moment the response is
    # sent. No /download link is offered for /api/process — callers are
    # expected to use the data in this response directly — so it's safe
    # to remove them immediately rather than waiting for the periodic
    # sweeper. This is what keeps disk usage flat under heavy automated
    # traffic instead of growing with every single API call.
    delete_file_quietly(os.path.join(OUTPUT_FOLDER, response_payload.get("json_filename", "")))
    if response_payload.get("records_filename"):
        delete_file_quietly(os.path.join(OUTPUT_FOLDER, response_payload["records_filename"]))

    response_payload["request_id"] = request_id
    return jsonify(response_payload)


@app.route("/download/<filename>", methods=["GET"])
def download_json(filename):
    """
    Lets the user download the generated JSON file by name.
    """
    return send_from_directory(OUTPUT_FOLDER, filename, as_attachment=True)


if __name__ == "__main__":
    # debug=True is fine for local development but should NOT be used
    # in the Docker/production image — the docker setup overrides this
    # by running via gunicorn instead of calling app.run() directly.
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)

