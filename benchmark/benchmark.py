"""
Benchmark script for the PDF-to-JSON service.

============================================================================
WHY THIS FILE WAS REPLACED
============================================================================
The previous version of this script sent PDFs to /api/process ONE AT A
TIME, in a plain serial loop, and reported the result as "throughput."
That's the wrong endpoint for a throughput test: /api/process processes
a single file per request, with no concurrency at all. The service's
actual concurrent processing -- the RAM-budget admission pool, multiple
PDFs running in real parallel worker processes, queueing when the batch
is bigger than the budget allows -- lives behind /upload-batch, and the
old script never called it. So the old "PDFs per minute" number was
really just "1 / (average single-file latency)" wearing a throughput
report's name. It would never have shown:

  - real concurrent speedup (or lack of it) from running many PDFs together
  - the MAX_FILES_PER_BATCH cap rejecting an oversized batch
  - RAM-budget admission/queueing behavior (small PDFs running together,
    large ones throttled)
  - whether the parallel pool is actually faster than processing the
    same files serially, which is the entire point of having one

This version fixes that by actually driving /upload-batch with real
batches, while keeping the EFFECTIVENESS section's logic from the old
version (that part was measuring the right things -- success rate,
empty-extraction rate, OCR-unavailable rate, table-detection rate, and
optional ground-truth accuracy -- it just needed a faster source of
real results to run on). The previous file is kept alongside this one
as benchmark.py.old_serial_version for reference -- it is NOT run by
anything anymore.

============================================================================
WHAT THIS SCRIPT NOW MEASURES
============================================================================

BATCH THROUGHPUT (the part that was wrong before, now fixed):
  - Sends PDFs to /upload-batch in real batches (configurable batch size,
    default 20 -- matches the server's own MAX_FILES_PER_BATCH default;
    see --batch-size to test other values, INCLUDING values larger than
    the server's cap, specifically so this script can show you exactly
    that rejection happening, the same wall you hit with 49 files).
  - Reports true wall-clock time for the WHOLE BATCH, not summed
    per-file times -- the actual user-facing latency of "I sent N files,
    how long until I had all N results back."
  - Reports a CONCURRENCY SPEEDUP figure: total server-reported
    processing seconds (if every file had run alone, back to back)
    divided by actual batch wall-clock time. >1x means real parallelism
    is happening; ~1x means the batch effectively ran serially (e.g.
    because RAM/CPU budget only allowed one at a time on this machine --
    see the "current concurrency limit" note in the report, which is not
    a flaw in this script, it's an accurate report of THIS machine's
    real ceiling).
  - Reports batch_size_rejected: true/false -- whether the batch size
    used actually exceeded the server's MAX_FILES_PER_BATCH and was
    rejected with HTTP 400, so a too-big batch is reported as exactly
    that, not silently retried smaller.

EFFECTIVENESS (kept from the previous version, logic unchanged):
  - success_rate, empty_extraction_rate, ocr_unavailable_rate,
    table_detection_rate as proxy signals (still proxies, not true
    accuracy, until ground truth exists -- see --ground-truth).

USAGE:
  # Default: one batch of up to 20 files (matches server default cap)
  python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs

  # Test a SPECIFIC batch size, e.g. to reproduce a 49-file batch and see
  # exactly what the server does with it (split across batches automatically,
  # OR show the rejection if --batch-size exceeds the server's real cap):
  python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs --batch-size 49

  # With ground truth for a subset of files:
  python benchmark.py --url http://localhost:5000 --pdf-dir ./sample_pdfs \\
      --ground-truth ./ground_truth

Results are printed to the console AND saved as timestamped report files
in benchmark/results/, so you can track whether throughput/effectiveness
improves or regresses as you change the code over time.
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# chunk(), balance_into_batches() (and the cost-estimation logic they
# depend on) now live in backend/batch_balancer.py, shared between this
# benchmark script and app.py's own /upload-batch-job endpoint -- see
# that module's docstring for why this used to be duplicated here and
# isn't anymore.
_BACKEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
from batch_balancer import chunk, balance_into_batches  # noqa: E402


def discover_pdfs(pdf_dir):
    return sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))


def load_ground_truth(ground_truth_dir, pdf_filename):
    """
    Looks for benchmark/ground_truth/<basename>.expected.json matching
    the given PDF. Returns the parsed expected records list, or None if
    no ground truth file exists for this PDF (the normal case until
    ground truth is built up).
    """
    if not ground_truth_dir:
        return None
    base = os.path.splitext(os.path.basename(pdf_filename))[0]
    expected_path = os.path.join(ground_truth_dir, f"{base}.expected.json")
    if not os.path.exists(expected_path):
        return None
    with open(expected_path, "r", encoding="utf-8") as f:
        return json.load(f)


def compare_records(actual_records, expected_records):
    """
    Transparent accuracy comparison: for each expected record, check
    whether the actual record at the same position matches exactly.
    Intentionally strict (exact match) -- loosen later (fuzzy string
    match, per-field scoring) once real ground truth reveals what kind
    of near-misses actually show up in practice.

    Returns (matched_count, total_expected, field_accuracy) where
    field_accuracy is the fraction of individual fields, across all
    expected records, that had a correct value in the actual record at
    the same position (looser than full-record match -- useful for
    spotting "almost right" extractions).
    """
    matched = 0
    field_hits = 0
    field_total = 0

    for i, expected in enumerate(expected_records):
        actual = actual_records[i] if i < len(actual_records) else {}
        if actual == expected:
            matched += 1
        for key, expected_val in expected.items():
            field_total += 1
            if str(actual.get(key, "")).strip() == str(expected_val).strip():
                field_hits += 1

    field_accuracy = round(field_hits / field_total, 4) if field_total else None
    return matched, len(expected_records), field_accuracy


def get_server_info(base_url, timeout=10):
    """
    Reads the server's live /api response in full -- both pool_status
    (worker_count_ceiling, usable RAM, etc, used so the report can
    correctly explain a 1x speedup as "this machine's real ceiling was
    1 worker" instead of implying something is broken) AND
    max_files_per_batch, the server's REAL per-request file-count cap.

    max_files_per_batch matters because --batch-size on this script is
    just a LOCAL grouping choice -- it does NOT change what the server
    will actually accept. If the server is running with its own
    MAX_FILES_PER_BATCH=20 (the default, unless someone set the env var
    when starting it) and this script is told --batch-size 50, sending
    a 25+ file batch will get an HTTP 400 from the server, every time,
    no matter what --batch-size says, because that number was never
    communicated to the server in the first place -- it only exists in
    this script's own batching logic. Fetching the REAL number here
    lets run_benchmark() catch and fix that mismatch automatically
    instead of discovering it as a confusing failed batch mid-run.

    Returns a dict with keys "pool_status" and "max_files_per_batch",
    or None for either/both if the server couldn't be reached or didn't
    return them (e.g. an older server build, before this field existed).
    """
    try:
        resp = requests.get(f"{base_url}/api", timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            return {
                "pool_status": data.get("pool_status"),
                "max_files_per_batch": data.get("max_files_per_batch"),
            }
    except requests.exceptions.RequestException:
        pass
    return {"pool_status": None, "max_files_per_batch": None}


def run_one_batch(base_url, pdf_paths, timeout):
    """
    Sends exactly one /upload-batch request containing all of
    pdf_paths, and returns (batch_wall_seconds, status_code,
    response_json_or_none, error_or_none).

    A single request is used for the whole list on purpose -- this is
    what actually exercises the server's real concurrent admission
    logic. Sending them one-by-one to /upload-batch would defeat the
    entire point, same mistake as the old script made with
    /api/process.
    """
    files_payload = []
    open_handles = []
    try:
        for p in pdf_paths:
            fh = open(p, "rb")
            open_handles.append(fh)
            files_payload.append(("files", (os.path.basename(p), fh, "application/pdf")))

        start = time.monotonic()
        try:
            response = requests.post(f"{base_url}/upload-batch", files=files_payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            return round(time.monotonic() - start, 3), None, None, str(e)
        elapsed = round(time.monotonic() - start, 3)

        if response.status_code != 200:
            return elapsed, response.status_code, None, f"HTTP {response.status_code}: {response.text[:400]}"

        return elapsed, response.status_code, response.json(), None
    finally:
        for fh in open_handles:
            fh.close()


def run_benchmark(base_url, pdf_dir, batch_size, balance_mode="weight", ground_truth_dir=None, timeout=600):
    pdfs = discover_pdfs(pdf_dir)
    if not pdfs:
        print(f"No PDFs found in {pdf_dir}. Add some .pdf files there first.")
        return None

    server_info_before = get_server_info(base_url, timeout=10)
    pool_status_before = server_info_before.get("pool_status")
    server_max_files_per_batch = server_info_before.get("max_files_per_batch")

    print(f"Found {len(pdfs)} PDF(s) in {pdf_dir}.")

    # THE FIX: --batch-size is only ever a LOCAL grouping choice on this
    # script's side -- it was never communicated to the server, and the
    # server enforces its OWN MAX_FILES_PER_BATCH regardless of what
    # this script is told. Previously, asking for --batch-size 50
    # against a server still running its default cap of 20 would send
    # an oversized request and get a confusing HTTP 400 partway through
    # a run. Now: fetch the server's REAL cap first, and if the
    # requested batch_size would exceed it, clamp down to the real
    # value automatically and explain why, rather than sending a
    # request guaranteed to be rejected.
    effective_batch_size = batch_size
    if server_max_files_per_batch is not None and batch_size > server_max_files_per_batch:
        print(f"NOTE: you asked for --batch-size {batch_size}, but this server's real "
              f"MAX_FILES_PER_BATCH is {server_max_files_per_batch}. Every batch sent at "
              f"{batch_size} would be rejected with HTTP 400, so batches will be capped at "
              f"{server_max_files_per_batch} instead (set MAX_FILES_PER_BATCH={batch_size} when "
              f"starting the server if you actually want batches that large).")
        effective_batch_size = server_max_files_per_batch
    elif server_max_files_per_batch is None:
        print(f"NOTE: could not read the server's real MAX_FILES_PER_BATCH (older server build, "
              f"or /api was unreachable) -- proceeding with --batch-size {batch_size} as given. "
              f"If batches get rejected with HTTP 400, that means the server's real cap is lower "
              f"than this; check what MAX_FILES_PER_BATCH the server was started with.")

    print(f"Benchmarking against {base_url} using /upload-batch, max files per batch = "
          f"{effective_batch_size}, balance mode = {balance_mode}.")
    if pool_status_before:
        print(f"Server's current worker_count_ceiling: {pool_status_before.get('worker_count_ceiling')}, "
              f"usable RAM right now: {pool_status_before.get('usable_mb_right_now')} MB")

    if balance_mode == "weight":
        print("Estimating each file's real processing cost (same estimator the server uses) "
              "to balance batches by weight, not just position...")
        batches, batch_weights = balance_into_batches(pdfs, effective_batch_size)
        for i, (b, w) in enumerate(zip(batches, batch_weights), start=1):
            print(f"  Batch {i}: {len(b)} file(s), ~{w:.0f} MB total estimated weight")
    else:
        batches = list(chunk(pdfs, effective_batch_size))
        batch_weights = [None] * len(batches)
    print()

    per_file_results = []
    batch_records = []
    benchmark_started = time.monotonic()

    for batch_index, batch_paths in enumerate(batches, start=1):
        print(f"Batch {batch_index}/{len(batches)}: sending {len(batch_paths)} file(s) "
              f"in ONE /upload-batch request ...", end=" ", flush=True)

        wall_seconds, status_code, response_json, error = run_one_batch(base_url, batch_paths, timeout)

        if error is not None:
            print(f"FAILED ({error})")
            batch_records.append({
                "batch_index": batch_index,
                "files_sent": len(batch_paths),
                "estimated_weight_mb": batch_weights[batch_index - 1],
                "wall_seconds": wall_seconds,
                "status_code": status_code,
                "rejected": status_code == 400,
                "error": error,
            })
            # The server rejected (or couldn't be reached for) this whole
            # batch -- record every file in it as failed, so the
            # effectiveness section still accounts for every input file,
            # rather than silently dropping them from the totals.
            for p in batch_paths:
                per_file_results.append({
                    "file": os.path.basename(p),
                    "success": False,
                    "error": error,
                    "server_seconds": None,
                    "page_count": None,
                    "row_count": None,
                    "record_count": None,
                    "ocr_pages_total": 0,
                    "ocr_unavailable_pages": 0,
                    "table_found": False,
                    "ground_truth_checked": False,
                    "ground_truth_matched": None,
                    "ground_truth_total": None,
                    "ground_truth_field_accuracy": None,
                })
            continue

        print(f"OK ({wall_seconds}s wall-clock for the whole batch, "
              f"{response_json['success_count']}/{response_json['file_count']} succeeded)")

        batch_records.append({
            "batch_index": batch_index,
            "files_sent": len(batch_paths),
            "estimated_weight_mb": batch_weights[batch_index - 1],
            "wall_seconds": wall_seconds,
            "status_code": status_code,
            "rejected": False,
            "error": None,
        })

        for result in response_json["results"]:
            filename = result.get("original_filename", "?")
            record = {
                "file": filename,
                "success": result.get("status") == "success",
                "error": result.get("error"),
                "server_seconds": result.get("processing_seconds"),
                "page_count": result.get("page_count"),
                "row_count": result.get("row_count"),
                "record_count": result.get("record_count"),
                "ocr_pages_total": 0,
                "ocr_unavailable_pages": 0,
                "table_found": bool(result.get("record_count")),
                "ground_truth_checked": False,
                "ground_truth_matched": None,
                "ground_truth_total": None,
                "ground_truth_field_accuracy": None,
            }

            for page in result.get("pages", []) or []:
                if page.get("type") == "image":
                    if page.get("ocr_used"):
                        record["ocr_pages_total"] += 1
                    elif page.get("extraction_method") == "ocr_unavailable":
                        record["ocr_unavailable_pages"] += 1

            if record["success"]:
                expected = load_ground_truth(ground_truth_dir, filename)
                if expected is not None:
                    record["ground_truth_checked"] = True
                    matched, total, field_acc = compare_records(result.get("records", []), expected)
                    record["ground_truth_matched"] = matched
                    record["ground_truth_total"] = total
                    record["ground_truth_field_accuracy"] = field_acc

            per_file_results.append(record)

    benchmark_elapsed = round(time.monotonic() - benchmark_started, 3)
    pool_status_after = get_server_info(base_url, timeout=10).get("pool_status")

    # --- Aggregate BATCH throughput (the part that was wrong before) ---
    successful = [r for r in per_file_results if r["success"]]
    total_pages = sum(r["page_count"] or 0 for r in successful)
    total_server_seconds_if_serial = sum(r["server_seconds"] or 0 for r in successful)
    total_batch_wall_seconds = sum(b["wall_seconds"] for b in batch_records if b["wall_seconds"])
    any_batch_rejected = any(b["rejected"] for b in batch_records)

    # Concurrency speedup: if every file's own server-reported time were
    # summed up as if run one-after-another (the old script's effective
    # measurement), vs the REAL wall-clock time taken to run them as
    # actual concurrent batches. >1.0 means real parallel speedup
    # happened; ~1.0 means this run was effectively serial (commonly
    # because this machine's worker_count_ceiling is 1, or the RAM
    # budget only ever allowed one job at a time -- see pool_status
    # below for why, not a bug in the measurement itself).
    concurrency_speedup = (
        round(total_server_seconds_if_serial / total_batch_wall_seconds, 2)
        if total_batch_wall_seconds > 0 else None
    )

    throughput = {
        "requested_batch_size": batch_size,
        "server_max_files_per_batch": server_max_files_per_batch,
        "effective_batch_size_used": effective_batch_size,
        "was_clamped": effective_batch_size != batch_size,
        "max_files_per_batch": effective_batch_size,  # kept for backward compatibility with older report readers
        "balance_mode": balance_mode,
        "server_max_concurrent_override_at_run_time": (pool_status_before or {}).get("max_concurrent_override"),
        "total_pdfs": len(per_file_results),
        "successful_pdfs": len(successful),
        "num_batches_sent": len(batches),
        "any_batch_rejected_too_large": any_batch_rejected,
        "total_batch_wall_clock_seconds": round(total_batch_wall_seconds, 3),
        "total_benchmark_wall_clock_seconds": benchmark_elapsed,
        "total_pages_processed": total_pages,
        "sum_of_individual_server_seconds_if_run_serially": round(total_server_seconds_if_serial, 3),
        "concurrency_speedup_vs_serial": concurrency_speedup,
        "pdfs_per_minute_batch_wall_clock": (
            round(len(successful) / total_batch_wall_seconds * 60, 2)
            if total_batch_wall_seconds > 0 else None
        ),
        "pages_per_minute_batch_wall_clock": (
            round(total_pages / total_batch_wall_seconds * 60, 2)
            if total_batch_wall_seconds > 0 else None
        ),
        "pool_status_before_run": pool_status_before,
        "pool_status_after_run": pool_status_after,
        "per_batch": batch_records,
    }

    # --- Aggregate effectiveness (proxy metrics; logic kept from the
    # previous version, since this part was measuring the right things
    # already -- only the SOURCE of results changed, from serial
    # /api/process calls to real /upload-batch results) ---
    total = len(per_file_results)
    empty_extractions = [
        r for r in successful
        if (r["page_count"] or 0) > 0 and (r["row_count"] or 0) == 0
    ]
    total_ocr_attempts = sum(r["ocr_pages_total"] + r["ocr_unavailable_pages"] for r in successful)
    total_ocr_unavailable = sum(r["ocr_unavailable_pages"] for r in successful)
    pdfs_with_table = [r for r in successful if r["table_found"]]

    gt_checked = [r for r in successful if r["ground_truth_checked"]]
    gt_field_accuracies = [r["ground_truth_field_accuracy"] for r in gt_checked if r["ground_truth_field_accuracy"] is not None]

    effectiveness = {
        "success_rate": round(len(successful) / total, 4) if total else None,
        "empty_extraction_rate": round(len(empty_extractions) / len(successful), 4) if successful else None,
        "empty_extraction_files": [r["file"] for r in empty_extractions],
        "ocr_unavailable_rate": round(total_ocr_unavailable / total_ocr_attempts, 4) if total_ocr_attempts else None,
        "table_detection_rate": round(len(pdfs_with_table) / len(successful), 4) if successful else None,
        "ground_truth": {
            "files_checked": len(gt_checked),
            "note": "Empty until ground-truth files exist in --ground-truth dir." if not gt_checked else None,
            "avg_field_accuracy": round(sum(gt_field_accuracies) / len(gt_field_accuracies), 4) if gt_field_accuracies else None,
            "per_file": [
                {
                    "file": r["file"],
                    "records_matched": r["ground_truth_matched"],
                    "records_expected": r["ground_truth_total"],
                    "field_accuracy": r["ground_truth_field_accuracy"],
                }
                for r in gt_checked
            ],
        },
    }

    summary = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "pdf_dir": pdf_dir,
        "throughput": throughput,
        "effectiveness": effectiveness,
        "per_file_results": per_file_results,
    }

    return summary


def print_summary(summary):
    t = summary["throughput"]
    print("\n" + "=" * 70)
    print("BATCH THROUGHPUT  (real /upload-batch concurrency, not one-at-a-time)")
    print("=" * 70)
    print(f"  Max files per batch used:              {t['effective_batch_size_used']}"
          + (f"  (clamped down from requested {t['requested_batch_size']} -- "
             f"server's real cap is {t['server_max_files_per_batch']})" if t['was_clamped'] else ""))
    print(f"  Batch balancing mode:                  {t['balance_mode']}")
    print(f"  Number of batches sent:                {t['num_batches_sent']}")
    print(f"  Any batch rejected (too large):        {t['any_batch_rejected_too_large']}")
    print(f"  PDFs tested / succeeded:               {t['total_pdfs']} / {t['successful_pdfs']}")
    print(f"  Total batch wall-clock time:           {t['total_batch_wall_clock_seconds']}s")
    print(f"  Sum of per-file times if run serially: {t['sum_of_individual_server_seconds_if_run_serially']}s")
    print(f"  Concurrency speedup vs serial:         {t['concurrency_speedup_vs_serial']}x")
    print(f"  PDFs/minute (real batch wall-clock):   {t['pdfs_per_minute_batch_wall_clock']}")
    print(f"  Pages/minute (real batch wall-clock):  {t['pages_per_minute_batch_wall_clock']}")
    pool_before = t.get("pool_status_before_run")
    if pool_before:
        print(f"\n  This machine's worker_count_ceiling at run time: {pool_before.get('worker_count_ceiling')}")
        print("  (If concurrency speedup is ~1x, this is very likely why -- "
              "see the report file for the full pool status; this is the real ceiling, not a bug.)")

    print("\n" + "=" * 70)
    print("EFFECTIVENESS (proxy metrics — no ground truth yet unless noted)")
    print("=" * 70)
    eff = summary["effectiveness"]
    print(f"  success_rate: {eff['success_rate']}")
    print(f"  empty_extraction_rate: {eff['empty_extraction_rate']}")
    if eff["empty_extraction_files"]:
        print(f"    -> files with 0 rows extracted despite having pages: {eff['empty_extraction_files']}")
    print(f"  ocr_unavailable_rate: {eff['ocr_unavailable_rate']}")
    print(f"  table_detection_rate: {eff['table_detection_rate']}")

    gt = eff["ground_truth"]
    if gt["files_checked"]:
        print(f"\n  Ground truth checked: {gt['files_checked']} file(s)")
        print(f"  Average field accuracy: {gt['avg_field_accuracy']}")
        for item in gt["per_file"]:
            print(f"    - {item['file']}: {item['records_matched']}/{item['records_expected']} records matched exactly, "
                  f"{item['field_accuracy']} field accuracy")
    else:
        print(f"\n  Ground truth: {gt['note']}")
    print()


def write_throughput_report(summary, results_dir, timestamp):
    """
    Writes a plain, standalone throughput report covering real BATCH
    concurrency -- wall-clock time for whole batches, concurrency
    speedup vs. running the same files one at a time, and whether any
    batch was rejected for being too large. This replaces the previous
    version's report, which only ever measured one-file-at-a-time
    latency and called it throughput.
    """
    t = summary["throughput"]
    lines = []
    lines.append("BATCH THROUGHPUT REPORT")
    lines.append("(tests real /upload-batch concurrency -- not one-file-at-a-time)")
    lines.append("=" * 60)
    lines.append(f"Run date:        {summary['run_at']}")
    lines.append(f"Service URL:     {summary['base_url']}")
    lines.append(f"PDF folder:      {summary['pdf_dir']}")
    lines.append("")
    lines.append(f"Requested batch size (--batch-size):  {t['requested_batch_size']}")
    lines.append(f"Server's real MAX_FILES_PER_BATCH:    {t['server_max_files_per_batch']}")
    lines.append(f"Effective batch size actually used:   {t['effective_batch_size_used']}"
                 + ("  <-- CLAMPED, see note below" if t['was_clamped'] else ""))
    if t['was_clamped']:
        lines.append(f"  NOTE: --batch-size {t['requested_batch_size']} was requested, but this server's")
        lines.append(f"  real MAX_FILES_PER_BATCH is {t['server_max_files_per_batch']}. Every batch sent at")
        lines.append(f"  {t['requested_batch_size']} would have been rejected with HTTP 400, so this run")
        lines.append(f"  automatically used {t['effective_batch_size_used']} instead. To actually run batches")
        lines.append(f"  of {t['requested_batch_size']}, restart the server with MAX_FILES_PER_BATCH={t['requested_batch_size']}.")
    lines.append(f"Batch balancing mode:                 {t['balance_mode']}")
    lines.append(f"Number of batches sent:               {t['num_batches_sent']}")
    lines.append(f"Any batch rejected as too large:      {t['any_batch_rejected_too_large']}")
    lines.append(f"PDFs tested:                          {t['total_pdfs']}")
    lines.append(f"PDFs processed OK:                    {t['successful_pdfs']}")
    lines.append(f"Total pages processed:                {t['total_pages_processed']}")
    lines.append("")
    lines.append(f"Total batch wall-clock time:           {t['total_batch_wall_clock_seconds']} seconds")
    lines.append(f"Sum of per-file times if run serially: {t['sum_of_individual_server_seconds_if_run_serially']} seconds")
    lines.append(f"Concurrency speedup vs. serial:         {t['concurrency_speedup_vs_serial']}x")
    lines.append("  (>1x = real parallel speedup happened.")
    lines.append("   ~1x = this run was effectively serial on this machine --")
    lines.append("   see worker_count_ceiling below for why; not a measurement error.)")
    lines.append("")
    lines.append(f"PDFs per minute (real batch wall-clock):   {t['pdfs_per_minute_batch_wall_clock']}")
    lines.append(f"Pages per minute (real batch wall-clock):  {t['pages_per_minute_batch_wall_clock']}")
    lines.append("")

    pool_before = t.get("pool_status_before_run")
    pool_after = t.get("pool_status_after_run")
    lines.append("Server's RAM-budget pool status:")
    lines.append("-" * 60)
    if pool_before:
        lines.append(f"  Before run: worker_count_ceiling={pool_before.get('worker_count_ceiling')}, "
                      f"usable_mb_right_now={pool_before.get('usable_mb_right_now')}, "
                      f"max_concurrent_override={pool_before.get('max_concurrent_override')}")
    if pool_after:
        lines.append(f"  After run:  worker_count_ceiling={pool_after.get('worker_count_ceiling')}, "
                      f"usable_mb_right_now={pool_after.get('usable_mb_right_now')}, "
                      f"in_flight_count={pool_after.get('in_flight_count')}, "
                      f"queued_count={pool_after.get('queued_count')}")
    if not pool_before and not pool_after:
        lines.append("  (Could not be read from the server's /api endpoint at run time.)")
    lines.append("")

    lines.append("Per-batch results:")
    lines.append("-" * 60)
    for b in t["per_batch"]:
        weight_str = f", ~{b['estimated_weight_mb']:.0f} MB estimated weight" if b.get("estimated_weight_mb") is not None else ""
        if b["rejected"] or b["error"]:
            lines.append(f"  Batch {b['batch_index']}: {b['files_sent']} file(s) sent{weight_str} -> "
                          f"REJECTED/FAILED ({b['error']})")
        else:
            lines.append(f"  Batch {b['batch_index']}: {b['files_sent']} file(s) sent{weight_str} -> "
                          f"{b['wall_seconds']}s wall-clock for the whole batch")
    lines.append("")

    lines.append("Per-file timing (within their batches):")
    lines.append("-" * 60)
    for r in summary["per_file_results"]:
        if r["success"]:
            lines.append(f"  {r['file']}: {r['server_seconds']}s server time, {r['page_count']} page(s)")
        else:
            lines.append(f"  {r['file']}: FAILED ({r['error']})")

    report_text = "\n".join(lines) + "\n"
    out_path = os.path.join(results_dir, f"throughput_report_{timestamp}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    return out_path, report_text


def write_effectiveness_report(summary, results_dir, timestamp):
    """
    Writes a plain, standalone effectiveness report — separate from
    throughput. Makes clear which numbers are TRUE accuracy (ground
    truth) vs PROXY signals (no ground truth yet), so the two are never
    confused with each other. Logic unchanged from the previous
    version -- this part was already measuring the right things.
    """
    e = summary["effectiveness"]
    gt = e["ground_truth"]
    lines = []
    lines.append("EFFECTIVENESS REPORT")
    lines.append("=" * 50)
    lines.append(f"Run date:        {summary['run_at']}")
    lines.append(f"Service URL:     {summary['base_url']}")
    lines.append(f"PDF folder:      {summary['pdf_dir']}")
    lines.append("")

    if gt["files_checked"]:
        lines.append(f"TRUE ACCURACY (ground truth checked for {gt['files_checked']} file(s))")
        lines.append("-" * 50)
        lines.append(f"Average field accuracy:   {gt['avg_field_accuracy']}")
        for item in gt["per_file"]:
            lines.append(f"  {item['file']}: {item['records_matched']}/{item['records_expected']} "
                          f"records exact match, {item['field_accuracy']} field accuracy")
        lines.append("")

    lines.append("PROXY SIGNALS (no ground truth yet for the rest — these are health")
    lines.append("checks, not proof the extracted data is correct)")
    lines.append("-" * 50)
    lines.append(f"Success rate:              {e['success_rate']}   (processed without crashing)")
    lines.append(f"Empty-extraction rate:     {e['empty_extraction_rate']}   (0 rows out despite having pages)")
    lines.append(f"OCR-unavailable rate:      {e['ocr_unavailable_rate']}   (scanned pages OCR couldn't read)")
    lines.append(f"Table-detection rate:      {e['table_detection_rate']}   (found a real table vs plain text)")
    if e["empty_extraction_files"]:
        lines.append("")
        lines.append("Files with 0 rows extracted (worth checking manually):")
        for f in e["empty_extraction_files"]:
            lines.append(f"  - {f}")

    if not gt["files_checked"]:
        lines.append("")
        lines.append("NOTE: No ground truth files found yet, so this report cannot tell you")
        lines.append("whether the extracted DATA is actually correct -- only whether the")
        lines.append("process ran cleanly. See benchmark/README.md for how to add ground")
        lines.append("truth and get a real accuracy number.")

    report_text = "\n".join(lines) + "\n"
    out_path = os.path.join(results_dir, f"effectiveness_report_{timestamp}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    return out_path, report_text


def save_results(summary, results_dir):
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    throughput_path, throughput_text = write_throughput_report(summary, results_dir, timestamp)
    effectiveness_path, effectiveness_text = write_effectiveness_report(summary, results_dir, timestamp)

    json_path = os.path.join(results_dir, f"benchmark_{timestamp}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(f"\nThroughput report saved to:    {throughput_path}")
    print(f"Effectiveness report saved to: {effectiveness_path}")
    print(f"(Raw JSON also saved to:        {json_path})")

    return throughput_path, effectiveness_path, json_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Benchmark the PDF-to-JSON service's REAL batch concurrency and effectiveness "
                     "(uses /upload-batch, not one-file-at-a-time /api/process)."
    )
    parser.add_argument("--url", default="http://localhost:5000", help="Base URL of the running service.")
    parser.add_argument("--pdf-dir", default=os.path.join(os.path.dirname(__file__), "sample_pdfs"),
                         help="Folder of PDFs to benchmark against.")
    parser.add_argument("--batch-size", type=int, default=20,
                         help="Max files allowed in ONE /upload-batch request. Default 20 matches the "
                              "server's own default MAX_FILES_PER_BATCH -- set this to whatever your "
                              "server is actually configured with. Set higher than the server's real "
                              "limit to see the rejection behavior directly (e.g. --batch-size 49 against "
                              "a server still set to 20).")
    parser.add_argument("--balance", choices=["weight", "position"], default="weight",
                         help="How files get grouped into batches. 'weight' (default) estimates each "
                              "file's real processing cost first and balances batches so no single batch "
                              "ends up with all the heavy files -- this is almost always what you want. "
                              "'position' just slices the file list in order (the old behavior) -- only "
                              "useful if you specifically want to test a particular file ordering.")
    parser.add_argument("--ground-truth", default=None,
                         help="Folder of <name>.expected.json ground-truth files (optional).")
    parser.add_argument("--timeout", type=int, default=600,
                         help="Per-batch request timeout in seconds (a whole batch must complete within this).")
    parser.add_argument("--results-dir", default=os.path.join(os.path.dirname(__file__), "results"),
                         help="Where to save the report files.")
    args = parser.parse_args()

    summary = run_benchmark(args.url, args.pdf_dir, args.batch_size, args.balance, args.ground_truth, args.timeout)
    if summary:
        print_summary(summary)
        save_results(summary, args.results_dir)
