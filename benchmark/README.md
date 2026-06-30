# Benchmark: throughput + effectiveness

This measures the two things Swapnil bhaiya asked for, once the
service is running somewhere (local Docker, Railway, Oracle Cloud —
doesn't matter, it just needs a URL):

- **Throughput** — how many PDFs/pages it processes per minute, **when
  actually sent as a real concurrent batch** (this used to be measured
  one file at a time — see "Why this changed" below; that's fixed now).
- **Effectiveness** — how often it extracts the right data correctly.

## Quick start

1. Make sure the service is running and reachable (e.g.
   `http://localhost:5000` or your Railway URL).
2. Put some test PDFs in `sample_pdfs/` — a realistic mix is best: a
   few plain-text PDFs, a few scanned/image PDFs, a few with real
   tables, a couple of edge cases (very large files, weird layouts).
3. Run:
   ```bash
   pip install requests
   python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs
   ```
   This sends ALL the PDFs in `sample_pdfs/` as real concurrent batches
   to `/upload-batch`. `--batch-size` (default 20) sets the most files
   you want grouped per request.

   **You don't need to match this to the server exactly anymore.** The
   script asks the server (`GET /api`) for its REAL `MAX_FILES_PER_BATCH`
   before sending anything. If `--batch-size` is higher than what the
   server actually allows, it gets clamped down automatically, with a
   clear note explaining what happened and how to raise the server's
   real limit if you actually want bigger batches:
   ```
   NOTE: you asked for --batch-size 50, but this server's real
   MAX_FILES_PER_BATCH is 20. Every batch sent at 50 would be rejected
   with HTTP 400, so batches will be capped at 20 instead (set
   MAX_FILES_PER_BATCH=50 when starting the server if you actually
   want batches that large).
   ```
   (Previously, asking for a bigger `--batch-size` than the server
   allowed would just send an oversized request and fail with a
   confusing HTTP 400 mid-run — this is now caught and handled before
   anything is sent.)

   **By default, files are grouped by estimated weight, not just
   position** (`--balance weight`, the default). Before sending
   anything, the script estimates every file's real processing cost
   using the same estimator the server itself uses
   (`backend/pdf_cost_estimator.py`), then spreads files across batches
   so no single batch ends up with all the heavy PDFs and another with
   all the light ones. This matters: with plain positional slicing, a
   folder of 4 huge scanned PDFs + 16 small ones could put all 4 huge
   files in the very first batch, making that one batch take far longer
   than the rest for no good reason — purely an artifact of file
   ordering, not anything real about the server's performance.

   If you specifically want the old positional behavior (e.g. to test a
   particular file ordering on purpose), use `--balance position`.

   Want to actually run bigger batches, e.g. 49 files at once? Start the
   server with that limit first, then match it on the benchmark side:
   ```bash
   # backend, when starting the server:
   MAX_FILES_PER_BATCH=49 python app.py

   # benchmark, in a separate terminal:
   python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs --batch-size 49
   ```
4. Read the console output, or open the saved report files in
   `results/` — `throughput_report_<timestamp>.txt` and
   `effectiveness_report_<timestamp>.txt` (plus a raw JSON with full
   per-file detail).

## Why this changed (if you saw an older version of this report)

The previous version of `benchmark.py` sent every PDF to
`/api/process` — the **single-file** endpoint — one at a time, in a
plain loop, and reported the result as "throughput." That number was
really just `1 / average single-file latency`, dressed up as a
throughput metric. It never touched `/upload-batch` (the endpoint with
the real worker pool and RAM-budget concurrency), so it could never
show real parallel speedup, never catch a too-large batch being
rejected, and never reflect what actually happens when many files are
sent together. The old file is kept as `benchmark.py.old_serial_version`
for reference; nothing runs it anymore.

The current version fixes this by sending real batches to
`/upload-batch` and reporting:
- **True batch wall-clock time** — how long the whole batch actually
  took, not summed individual times.
- **Concurrency speedup vs. serial** — actual wall-clock time vs. what
  it would have taken if every file ran one after another. A number
  near 1x on a given machine usually means that machine's
  `worker_count_ceiling` (visible in the report) is 1 — that's an
  honest report of that machine's real ceiling, not a bug in the
  measurement.
- **Whether a batch got rejected for being too large** (`any_batch_
  rejected_too_large`), and the server's exact error message when it
  does — so a batch-size problem shows up directly in the report
  instead of going unnoticed.

## What "effectiveness" means right now (and what it doesn't yet)

True accuracy means: for a given PDF, you already know the correct
answer, and you check the tool's output against it. **That ground
truth doesn't exist yet for this project** — building it means
manually reviewing a PDF once and writing down what the correct
extracted records should look like.

Until that exists, the script reports **proxy health signals** instead
— numbers that correlate with quality and catch real problems, without
needing a known-correct answer:

| Metric | What it means | Why it's a useful proxy |
|---|---|---|
| `success_rate` | % of PDFs processed without crashing/erroring | Crashes are an obvious effectiveness floor |
| `empty_extraction_rate` | % of PDFs where 0 rows came out despite having pages | Almost always means something went wrong, not that the PDF was genuinely empty |
| `ocr_unavailable_rate` | % of image-only pages that couldn't be read because OCR wasn't installed | Flags environment/setup problems, not extraction logic problems |
| `table_detection_rate` | % of PDFs where a real structured table was found (vs. falling back to plain text lines) | Useful context — a low rate isn't necessarily bad if most of your PDFs genuinely don't contain tables |

These are real signals, but they are **not** the same as knowing the
extracted data is *correct*. A PDF can have a 100% success rate, find
a table, extract a healthy number of rows — and still have wrong values
in some cells. Proxy metrics can't catch that. Only ground truth can.

## Adding ground truth (recommended next step)

Once you're ready to measure true accuracy:

1. Pick a handful of representative PDFs — ideally ones that cover
   your real use cases (different table layouts, scanned pages, etc.)
2. Run them through the tool once, and manually check/correct the
   output — confirm every record's every field is actually right.
3. Save the corrected version as `ground_truth/<pdf-basename>.expected.json`,
   matching the shape of the `records` field in the API output:
   ```json
   [
     { "id": "92335", "date": "06.08.2026", "company": "Transocean", "whatWasSeen": "..." },
     { "id": "92336", "date": "06.08.2026", "company": "Scantech", "whatWasSeen": "..." }
   ]
   ```
4. Re-run the benchmark with `--ground-truth`:
   ```bash
   python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs --ground-truth ./ground_truth
   ```
5. The output will now include, for each file with a matching ground
   truth file: how many records matched exactly, and a field-level
   accuracy percentage (catches "almost right" extractions, not just
   perfect-or-nothing).

The comparison logic is intentionally simple (exact string match per
field) so it's easy to reason about. If real PDFs turn out to have a
lot of "close but not exact" mismatches (e.g. trailing whitespace,
date format differences), the matching logic in `compare_records()`
inside `benchmark.py` is the place to loosen it — e.g. normalize dates,
ignore case, or use fuzzy string matching.

## Tracking progress over time

Every run saves a timestamped JSON file to `results/`. Since each
result includes the full `per_file_results` breakdown, you can diff
two runs (e.g. before/after a code change) to see exactly which files
got faster, slower, or changed in row/record counts — useful for
catching regressions before they reach production.
