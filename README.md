# PDF to JSON Converter

A full-stack app: upload a PDF, get back a structured JSON file (same
name as the PDF). No AI APIs are used — only traditional, deterministic
parsing/OCR libraries.

The core feature: **every page is checked individually** for whether it
contains a real (selectable) text layer, an embedded raster image
(scanned look), both, or neither — instead of guessing once for the
whole document. Mixed PDFs (e.g. a report with some typed pages and
some scanned pages) are handled correctly.

## Project Structure

```
pdf-to-json-app/
├── start.bat                # Double-click this on Windows
├── start.sh                 # Double-click this on Mac/Linux
├── Dockerfile                # Container build (Python + Tesseract + Ghostscript + Poppler)
├── docker-compose.yml         # Easiest way to build+run the container locally
├── railway.json               # Railway deployment config (one-click deploy from GitHub)
├── .dockerignore
├── backend/
│   ├── app.py              # Flask server + per-page classification + extraction + API
│   └── requirements.txt    # Python dependencies
├── frontend/
│   ├── index.html          # Upload UI (file OR URL) + per-page breakdown table
│   ├── style.css           # Styling
│   └── script.js           # Calls the backend API, renders the breakdown
├── benchmark/
│   ├── benchmark.py         # Throughput + effectiveness measurement script
│   ├── README.md            # How to use the benchmark, including ground truth
│   ├── sample_pdfs/          # Put PDFs here to benchmark against
│   ├── ground_truth/         # Optional: <name>.expected.json answer keys
│   └── results/               # Timestamped JSON results land here
├── uploads/                 # Uploaded PDFs are stored here
└── outputs/                  # Generated JSON files are stored here
```

## How page detection works

For every page, two independent signals are checked:

1. **Does it have a real text layer?** (selectable/copyable text, not
   pixels that merely look like text)
2. **Does it have an embedded raster image?** (a photo or a full-page
   scan placed on the page)

This is done with **PyMuPDF (`fitz`)** — `page.get_text()` checks for a
real text layer, `page.get_images()` checks for embedded raster images.
This is the fastest and most reliable way to tell scanned pages apart
from real text, because it inspects the PDF's internal structure
directly rather than guessing from rendered pixels.

If PyMuPDF isn't installed, the app automatically falls back to
**pdfplumber** (`page.chars` for text glyphs, `page.images` for raster
images) — slightly slower, still accurate.

Each page is then labeled:

| Label    | Has text | Has image | Meaning                                  |
|----------|----------|-----------|-------------------------------------------|
| `text`   | ✅       | ❌        | Normal typed page                         |
| `image`  | ❌       | ✅        | Scanned / photographed page, no real text |
| `mixed`  | ✅       | ✅        | Typed text AND a picture on the same page |
| `empty`  | ❌       | ❌        | Blank page                                |

## How extraction works (per page, not per document)

Once a page is labeled, the extractor that actually fits that page's
content is used — not a single whole-document guess:

- **`text` / `mixed` pages** → tries **Camelot** first, in case the
  page contains a real table (validated — Camelot's borderless "stream"
  mode can otherwise misread normal paragraphs as a fake table; this is
  checked and rejected if so). If no real table is found, plain
  **pdfplumber** text extraction is used instead.
- **`image` pages** → **OCR (Tesseract)**, since there's no text layer
  to read directly. Only runs if OCR libraries are installed.
- **`empty` pages** → nothing to extract.

The saved output now includes TWO JSON files per upload:

- **`<name>.json`** — full detail: per-page breakdown (`pages`) plus a
  flat row-by-row view (`data`, one pipe-joined string per table/text
  line) plus the clean structured `records` array (same content as
  the second file below, included here too for convenience).
- **`<name>.records.json`** — ONLY produced when a real table was
  detected. A plain array of clean objects, one per table row, with
  column headers turned into camelCase keys and empty cells dropped
  entirely. This is the ready-to-use data file. For multi-page tables
  where the header row only appears once (e.g. a long report), the
  same headers are automatically reused for every later page that
  continues the same table.

Example `<name>.records.json`:
```json
[
  {
    "id": "92335",
    "date": "06.08.2026",
    "company": "Transocean",
    "whatWasSeen": "..."
  },
  {
    "id": "92336",
    "date": "06.08.2026",
    "company": "Scantech",
    "whatWasSeen": "...",
    "commentsAddedByRigLeadership": "..."
  }
]
```

The full `<name>.json` always includes a `pages` array reporting the
classification and extraction method used for every page:

```json
{
  "source_file": "report.pdf",
  "page_count": 3,
  "page_type_counts": { "text": 1, "image": 1, "mixed": 1, "empty": 0 },
  "pages": [
    { "page": 1, "type": "text",  "has_text": true,  "has_image": false, "extraction_method": "text_layer" },
    { "page": 2, "type": "image", "has_text": false, "has_image": true,  "extraction_method": "ocr" },
    { "page": 3, "type": "mixed", "has_text": true,  "has_image": true,  "extraction_method": "text_layer" }
  ],
  "records": [ /* clean structured objects, if a table was found */ ],
  "data": [ /* flat row-by-row view */ ]
}
```

Both files are saved as `outputs/<same-name-as-pdf>.json` and
`outputs/<same-name-as-pdf>.records.json`.

## How table records are cleaned up

Two extra fixes are applied automatically to the structured `records`
output (the `<name>.records.json` file):

1. **Every record always has every column key**, even if a cell was
   empty — empty cells are written as `""` instead of being omitted.
   This keeps every record's key set identical, which matters for
   loading the output into Excel or a database with consistent columns.
   (A row that is *entirely* blank across all columns is still skipped
   — only genuinely blank rows are dropped, not partially-filled ones.)

2. **Narrow merged-column repair.** Some PDF tables have a column whose
   value is always the same short constant (e.g. a rig code like
   "TNG") in a column too narrow for Camelot to reliably keep separate
   from its neighbor. This can cause the constant to get glued onto the
   front of a neighboring column's text on some rows, while the narrow
   column's own key goes missing on those rows entirely. The app
   detects this automatically — if the same short word appears as a
   clean leading prefix across more than one different column, and in
   nearly every record, it's treated as a leaked narrow column: a clean
   value is written into its own field for every record, and the
   leaked prefix is stripped from wherever it landed. This never
   guesses at moving the REST of a field's text to a different
   column — only the leaked prefix itself is touched, so no real data
   is fabricated or misplaced.

## Setup & Run Instructions (the easy way)

You need **Python 3.9+** installed on your computer first
(https://www.python.org/downloads/ — if you don't have it).

That's the only thing you need to install manually. Everything else is
automatic.

### Every time you want to use the app:

- **Windows:** double-click `start.bat`
- **Mac/Linux:** double-click `start.sh` (or run `./start.sh` in a terminal)

The first time you run it, it will automatically:
1. Create a virtual environment
2. Install all required Python libraries (including PyMuPDF, used for
   page classification)
3. Start the backend server
4. Open the upload page in your browser

Every time **after** that, it just starts the server and opens the page —
no installing, no typing commands, nothing to remember.

Leave the black terminal window open while you use the app. Closing it
stops the server.

### Using the app
1. Click "Choose File" and select a PDF.
2. Click "Upload & Convert".
3. Wait for the "Processing..." indicator.
4. You'll see:
   - Summary chips (counts of text / image / mixed / empty pages)
   - A per-page breakdown table (type, has text, has image, image
     count, extraction method, lines extracted)
   - A preview of the extracted JSON rows
   - A "Download JSON" button
5. Click it to download `<your-pdf-name>.json`.

### Required: Tesseract OCR + Ghostscript (for full accuracy)

For complete accuracy across all page types, two system-level tools are
needed alongside the Python packages:

**Tesseract OCR** (reads text out of scanned/image-only pages):
- Windows: https://github.com/UB-Mannheim/tesseract/wiki
- Mac: `brew install tesseract`
- Linux: `sudo apt-get install tesseract-ocr`

`pdf2image` (used to render pages for OCR) also needs **poppler**:
- Mac: `brew install poppler`
- Linux: `sudo apt-get install poppler-utils`
- Windows: download poppler binaries and add them to PATH.

**Ghostscript** (lets Camelot detect real tables on text/mixed pages):
- Windows: https://www.ghostscript.com/releases/gsdnld.html
- Mac: `brew install ghostscript`
- Linux: `sudo apt-get install ghostscript`

Both are optional in the sense that the app won't crash without them —
without Tesseract, `image` pages are reported with the correct
classification but empty extracted text; without Ghostscript, tables
fall back to plain line-by-line text. But installing both gives the
most accurate results across every page type.

## API Reference

These endpoints work identically whether the app is running locally
(`python app.py`) or inside Docker — only the base URL changes.

### `POST /upload`
For the web frontend, but also fine for direct API calls. Accepts
EITHER of:
- Form-data field `file` (the PDF), **or**
- Form-data field `url` (a direct link to a PDF — the server downloads
  it itself, no human upload needed)

Returns: JSON with `json_filename`, `records_filename` (null if no
table was found), `row_count`, `record_count`, `page_count`,
`page_type_counts`, `processing_seconds`, `pages_per_second`, `pages`
(per-page breakdown), `records` (clean structured objects), and `data`
(the flat row-by-row view).

### `POST /upload-batch`
Like `/upload`, but for **multiple PDFs in one request**. Send one or
more files under repeated `files` form-data fields:

```bash
curl -X POST https://your-deployed-url.com/upload-batch \
  -F "files=@/path/to/report1.pdf" \
  -F "files=@/path/to/report2.pdf" \
  -F "files=@/path/to/report3.pdf"
```

The server processes up to `pdf_worker_pool_size` of these **genuinely
in parallel**, in separate worker processes — any files beyond that
capacity queue automatically and pick up a worker as soon as one frees
up. `pdf_worker_pool_size` is decided automatically at server startup
based on this specific machine/container's real available RAM and CPU
(see `GET /api` for the live breakdown of how that number was chosen —
it adapts on its own to wherever this is deployed, no configuration
needed).

Returns a JSON object:
```json
{
  "message": "Processed 3 file(s): 3 succeeded, 0 failed.",
  "file_count": 3,
  "success_count": 3,
  "error_count": 0,
  "results": [
    { "status": "success", "original_filename": "report1.pdf", "json_filename": "...", "row_count": 115, "...": "...same shape as /upload" },
    { "status": "success", "original_filename": "report2.pdf", "...": "..." },
    { "status": "error", "original_filename": "report3.pdf", "error": "Processing failed: ..." }
  ]
}
```
`results` is always in the **same order** the files were sent, never
completion order — even when two files in the batch share the exact
same original filename, each gets tracked and returned correctly and
independently. One bad/corrupt PDF in the batch returns a `status:
"error"` entry for just that file; it does not prevent the rest of the
batch from completing successfully.

By default, a single `/upload-batch` request is capped at
`MAX_FILES_PER_BATCH` (20) files, and rejects the whole request with
HTTP 400 if exceeded. To send more than that in one request, either
send multiple separate `/upload-batch` requests yourself, or use
`/upload-batch-job` below, which does that splitting for you.

### `POST /upload-batch-job`
Same idea as `/upload-batch`, but accepts **more files than
`MAX_FILES_PER_BATCH` in a single request** — the server splits them
into multiple internal sub-batches automatically and processes each
one in turn, so the caller doesn't need to implement any splitting
logic themselves:

```bash
# Works even with 100 files in one request, where /upload-batch
# would reject anything over MAX_FILES_PER_BATCH (20 by default):
curl -X POST https://your-deployed-url.com/upload-batch-job \
  -F "files=@report1.pdf" -F "files=@report2.pdf" -F "files=@report3.pdf" \
  # ... up to as many files as fit under MAX_PDF_BYTES total
```

Files are split into sub-batches using the **same weight-balancing
algorithm** the benchmark script uses (`backend/batch_balancer.py`) —
each file's likely processing cost is estimated first, so no single
sub-batch ends up with all the heavy/scanned PDFs while another gets
all the light ones (which would otherwise make that one sub-batch take
far longer than the rest, purely from how the files happened to be
grouped).

Returns once every sub-batch has finished, in the same response shape
as `/upload-batch`, plus a `sub_batches` field showing exactly how the
files were grouped:
```json
{
  "message": "Processed 30 file(s) across 2 internal sub-batch(es): 30 succeeded, 0 failed.",
  "file_count": 30,
  "success_count": 30,
  "error_count": 0,
  "sub_batches": [
    { "sub_batch_index": 1, "file_count": 15, "estimated_weight_mb": 600.0 },
    { "sub_batch_index": 2, "file_count": 15, "estimated_weight_mb": 600.0 }
  ],
  "results": [ "...same per-file shape as /upload-batch, in original input order..." ]
}
```

**Important:** this does NOT raise the total request-size cap
(`MAX_PDF_BYTES` / `MAX_CONTENT_LENGTH`) — Flask still rejects the
whole request with HTTP 413 if the combined size of all files exceeds
that limit, before this endpoint's code even runs. Raise
`MAX_PDF_BYTES` separately if you need a bigger total payload across
many files (see the environment variables table below).

### `POST /api/process`
The endpoint built for **other tools/services to call automatically**
— no human clicking "upload." Two ways to call it:

**1. By URL (JSON body):**
```bash
curl -X POST https://your-deployed-url.com/api/process \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/report.pdf"}'
```

**2. By file upload (multipart):**
```bash
curl -X POST https://your-deployed-url.com/api/process \
  -F "file=@/path/to/report.pdf"
```

Response shape is identical to `/upload`, plus a `request_id` field so
you can correlate logs/results across automated calls. Every response
includes `processing_seconds`, which is what the benchmark script uses
to measure throughput.

**Note on URL support:** only plain, direct links to a PDF are
supported right now (e.g. `https://host/file.pdf`) — not Google Drive
or Dropbox "share" links, which return an HTML viewer page rather than
the raw PDF bytes. If a URL doesn't return real PDF content, the API
returns a clear `400` error explaining why, rather than failing
silently.

### `GET /download/<filename>`
Downloads the generated JSON file by name (e.g. `/download/report.json`)

### `GET /health`
Returns `{"status": "ok", "pymupdf_available": ..., "ocr_available": ...}`.
Used by Docker's health check and by hosting platforms (Railway,
Oracle Cloud) to confirm the service is actually serving requests, not
just running.

## Running with Docker

Docker bundles everything the app needs — Python, Tesseract OCR,
Ghostscript, Poppler — into one container, so it behaves identically
on your laptop and on a server. You don't need to manually install any
of those four tools yourself anymore.

**Requires:** [Docker](https://docs.docker.com/get-docker/) installed
on your machine (free).

### Build and run locally
```bash
docker compose up --build
```
This builds the image and starts the service at `http://localhost:5000`.
Generated `uploads/` and `outputs/` are saved to your local folder so
you can inspect them, even after the container stops.

To stop it: `Ctrl+C`, then `docker compose down`.

### Build and run without docker-compose
```bash
docker build -t pdf-to-json .
docker run -p 5000:5000 pdf-to-json
```

### Configuration (environment variables)
| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `5000` | Port the server listens on |
| `WEB_CONCURRENCY` | `1` | Number of gunicorn **web** worker processes (handles HTTP I/O). Leave this at `1` — it is NOT what controls how many PDFs process in parallel; see `MAX_CONCURRENT_PDFS` below for that. |
| `GUNICORN_TIMEOUT` | `300` | Max seconds per request (large/OCR-heavy PDFs, or a big batch queued behind a small pool, can take a while) |
| `MAX_CONCURRENT_PDFS` | _(auto-detected)_ | Manual ceiling on how many PDFs process **genuinely at the same time**, in separate worker processes. By default this is decided automatically at startup from this machine/container's real available RAM and CPU (see `GET /api` for the live numbers it computed) — set this only if you want to force a lower limit than what was auto-detected, e.g. to leave headroom for other things running on the same box. |
| `MAX_FILES_PER_BATCH` | `20` | Largest number of files accepted in one `/upload-batch` request. Files beyond `MAX_CONCURRENT_PDFS` within an accepted batch simply queue, so this cap exists to stop one request from queuing an unreasonable number of files, not to limit total parallelism. |
| `MAX_PDF_BYTES` | `524288000` (500MB) | Largest **total request body** accepted, by upload or URL — for `/upload-batch` and `/upload-batch-job`, this is the sum of every file in the batch, the same way it's the size of the one file for `/upload`. Raised from an original 50MB default, which proved too tight for realistic multi-file batches. This is intentionally just a blunt request-level backstop — the finer-grained protection against memory exhaustion is `RamBudgetPool` (see `backend/ram_budget_pool.py`), which checks real live available RAM against each PDF's own estimated cost. Lower this (e.g. to 20–50MB) if deploying on a memory-constrained tier like Railway's free plan; raise it further if your server's RAM supports it |
| `URL_FETCH_TIMEOUT_SECONDS` | `30` | How long to wait when downloading a PDF from a URL |
| `FILE_RETENTION_HOURS` | `6` | How long generated files are kept on disk before automatic deletion (see "Automatic file cleanup" below) |
| `CLEANUP_INTERVAL_SECONDS` | `1800` (30 min) | How often the background cleanup sweep runs |

### How many PDFs can run in parallel?
This is decided automatically, at server startup, based on the actual
machine/container running it — **not** a fixed number baked into the
code. Check `GET /api` on a running instance for the exact number it
chose and the full reasoning (available RAM, available CPU cores, and
which one ended up being the limiting factor). The same unmodified
Docker image will correctly detect a different safe number on a 4-core
laptop vs. an 8-core server vs. a memory-constrained free-tier host —
including correctly respecting `docker run --cpus=N` limits, which a
naive core-count check would otherwise ignore.

### Automatic file cleanup

On a public deployment, every request — from every user — leaves a PDF
in `uploads/` and a JSON file in `outputs/`. Without cleanup, disk
usage would grow forever and eventually fill up the server. Two
cleanup mechanisms run automatically, no setup needed:

1. **`/api/process` deletes its own output files immediately** after
   sending the response. This endpoint already returns the full
   extracted data directly in the response body, so the on-disk copy
   serves no purpose afterward — there's no `/download` link offered
   for this endpoint, since callers are expected to use the response
   data directly.

2. **`/upload` (the web frontend) keeps files for a while, then a
   background sweeper cleans them up.** The frontend's "Download JSON"
   button needs the file to still exist on the server when clicked, so
   it isn't deleted immediately. Instead, a background thread checks
   every `CLEANUP_INTERVAL_SECONDS` and deletes anything older than
   `FILE_RETENTION_HOURS` (default: 6 hours) from both `uploads/` and
   `outputs/`. A cleanup pass also runs once immediately on server
   startup, so leftover files from before a restart/redeploy don't
   linger indefinitely.

This means: **the files a user sees in their browser's Downloads
folder are a real copy on their own machine** — once downloaded,
deleting the server-side copy later doesn't affect their downloaded
file at all. The cleanup only removes the temporary server-side copy
that existed so the download link could work.

## Deploying publicly (Railway or Oracle Cloud)

Right now the app only runs on `127.0.0.1` — your own machine. To make
it reachable by anyone (or by another automated tool), it needs to run
on a server with a public URL. Both options below work from the same
`Dockerfile` — no extra setup needed beyond what's already in this repo.

### Option A: Railway (simplest)
1. Push this project to a GitHub repository.
2. Go to [railway.app](https://railway.app) → New Project → **Deploy from GitHub repo**.
3. Select this repo. Railway detects the `Dockerfile` and `railway.json`
   automatically and builds the container.
4. Once deployed, Railway gives you a public URL like
   `https://your-app.up.railway.app`.
5. Test it: `curl https://your-app.up.railway.app/health`

**Heads up on Railway's actual free tier (checked June 2026):** Railway
no longer has a no-strings-attached free tier. New accounts get a
one-time $5 trial credit (requires a credit card to start, even for
the trial) good for about 30 days. After that, the ongoing free option
drops to just 0.5 GB RAM / 1 vCPU, which is tight for this app — Camelot,
OCR, and PyMuPDF together use real memory, especially on large
multi-page scanned PDFs, so it's possible to hit out-of-memory crashes
on that smallest tier. The paid Hobby plan ($5/month minimum) gives
more headroom. Worth checking railway.com/pricing yourself before
relying on this for anything beyond a quick demo.

**Known limitation: Railway enforces a hard 5-minute timeout on every
public HTTP request**, with no way to configure it higher. For most
PDFs this is plenty — but a large, multi-page, scanned document
running through OCR could realistically take longer than 5 minutes,
and Railway will cut the connection at that point regardless of
whether the server is still working. If you expect to process large
scanned documents regularly, Oracle Cloud (Option B below) doesn't
have this limit, since it's a real VM rather than a managed platform.

### Option B: Oracle Cloud (more control, more setup)
1. Create an "Always Free" compute instance (Oracle's free tier
   includes small VMs indefinitely, not just a trial period).
2. Install Docker on the instance:
   ```bash
   sudo apt-get update && sudo apt-get install -y docker.io
   sudo systemctl enable --now docker
   ```
3. Copy this project to the instance (`git clone` your repo, or `scp`
   the folder over).
4. Build and run:
   ```bash
   sudo docker build -t pdf-to-json .
   sudo docker run -d -p 80:5000 --restart unless-stopped pdf-to-json
   ```
5. Open port 80 in Oracle Cloud's security list/firewall rules for
   the instance (this step trips people up — it's a separate setting
   from the OS firewall, configured in the Oracle Cloud console under
   the instance's VCN security rules).
6. Your public IP becomes the URL: `http://<your-instance-public-ip>/health`

Oracle Cloud gives more control (a real Linux VM, no time-based free
tier limits) but needs more manual setup than Railway.

## Measuring throughput and effectiveness (benchmark.py)

Once the service is running somewhere (locally, Railway, or Oracle
Cloud), `benchmark/benchmark.py` measures:

- **Throughput** — PDFs per minute, pages per minute, average seconds
  per PDF.
- **Effectiveness** — for now, proxy health signals (success rate,
  rate of suspicious zero-row extractions, OCR-unavailable rate, table
  detection rate) since true accuracy needs a ground-truth answer key,
  which doesn't exist yet for this project. The script is already
  wired up to report TRUE accuracy automatically for any file you add
  ground truth for — see `benchmark/README.md`.

```bash
cd benchmark
pip install requests
python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs
```

Drop your test PDFs into `benchmark/sample_pdfs/` first. Results print
to the console and also get saved as a timestamped JSON file in
`benchmark/results/`, so you can track whether throughput/effectiveness
improves or regresses as the extraction code changes over time.

See `benchmark/README.md` for full details, including how to add
ground truth once you have it.

