"""
Weight-balanced batch splitting, shared by:
  - app.py's new /upload-batch-job endpoint (splits a large incoming
    file list into server-side sub-batches automatically)
  - benchmark.py (splits a folder of test PDFs into client-side
    sub-batches before sending each one as a separate /upload-batch
    request)

WHY THIS LIVES HERE, NOT DUPLICATED IN BOTH PLACES:
This logic used to exist only inside benchmark.py. When the server
itself needed the same "split into balanced groups of at most N files"
behavior (for /upload-batch-job, which accepts more files than
MAX_FILES_PER_BATCH and processes them as multiple internal batches),
duplicating the algorithm into app.py would have meant two copies that
could quietly drift apart over time. Pulling it into backend/, next to
pdf_cost_estimator.py (which it depends on), lets both the server and
the benchmark script import the exact same, single implementation.

See balance_into_batches() for the full algorithm explanation.
"""
import os


def chunk(items, size):
    """
    Splits a list into consecutive chunks of at most `size` items, by
    POSITION only -- ignores file size completely. Kept only for
    balance_mode="position" callers; see balance_into_batches for the
    default, size-aware behavior. If files happen to be sorted small
    first and large last, this can accidentally put all the heavy
    files in one batch and all the light ones in another -- the worst
    possible grouping for even batch timing.
    """
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _estimate_all(pdf_paths):
    """
    Runs the cost estimator (pdf_cost_estimator.py, same module the
    server's RamBudgetPool itself uses) against every file. This is
    cheap (structural inspection only) and lets batches be grouped by
    real predicted weight instead of guessing from file size on disk
    (a PDF's byte size on disk is a poor proxy for its actual
    processing cost -- a small file full of big embedded images can
    cost far more RAM than a large file that's mostly plain text).

    Returns a list of (path, estimated_mb) tuples, same order as input.
    On any estimation failure for a given file, falls back to a
    moderate constant (280MB, matching pdf_cost_estimator's own
    fallback) rather than crashing the whole batching step over one
    bad file.
    """
    try:
        from pdf_cost_estimator import estimate_pdf_cost_mb
    except ImportError:
        # Estimator module not importable for some reason -- fall back
        # to file size on disk as a rough proxy, clearly worse than the
        # real estimator but still better than ignoring size completely.
        results = []
        for p in pdf_paths:
            try:
                size_mb = os.path.getsize(p) / (1024 * 1024)
            except OSError:
                size_mb = 1.0
            results.append((p, max(1.0, size_mb)))
        return results

    results = []
    for p in pdf_paths:
        try:
            est_mb, _ = estimate_pdf_cost_mb(p)
        except Exception:
            est_mb = 280.0
        results.append((p, est_mb))
    return results


def balance_into_batches(pdf_paths, max_files_per_batch):
    """
    Groups files into batches that respect max_files_per_batch AND are
    balanced by TOTAL ESTIMATED RAM WEIGHT, not just by file count.

    WHY WEIGHT-BALANCED, NOT JUST COUNT-BALANCED:
    Splitting files into batches of "however many fit under the cap"
    by POSITION ALONE (see chunk() above) can accidentally put every
    large/scanned PDF in one batch and every small one in another.
    That makes one batch take far longer than the others for no good
    reason -- purely an artifact of input ordering, not anything real
    about processing capacity.

    ALGORITHM (greedy "Longest Processing Time first" bin-balancing --
    a standard, simple, well-understood approach to this exact kind of
    scheduling problem):
      1. Estimate every file's real cost (_estimate_all above).
      2. Decide how many batches are needed:
         ceil(total_files / max_files_per_batch) -- a hard floor
         coming directly from the real per-request file-count cap.
      3. Sort files HEAVIEST FIRST.
      4. Walk through files heaviest-to-lightest. For each one, place
         it into whichever batch CURRENTLY has the lowest total
         estimated weight, skipping any batch that's already full
         (hit max_files_per_batch).
      5. Result: every batch respects the hard file-count cap, AND
         batches end up close to equal in total estimated weight, so
         no single batch is unfairly "all the big ones."

    Returns (batches, weights) where batches is a list of lists of
    paths, and weights is the parallel list of each batch's total
    estimated MB.
    """
    if not pdf_paths:
        return [], []

    estimates = _estimate_all(pdf_paths)  # [(path, mb), ...] same order as input
    total_files = len(estimates)
    num_batches = max(1, -(-total_files // max_files_per_batch))  # ceil division

    # Heaviest-first -- this is what makes greedy bin-balancing work
    # well: placing big items first, while every batch still has room
    # to absorb the imbalance, gives a much more even result than
    # placing small items first and hoping it averages out.
    estimates_sorted = sorted(estimates, key=lambda pair: pair[1], reverse=True)

    batches = [[] for _ in range(num_batches)]
    batch_weights = [0.0] * num_batches

    for path, est_mb in estimates_sorted:
        # Among batches that still have room (haven't hit the file-count
        # cap), pick the one with the smallest running total so far.
        candidate_indices = [i for i in range(num_batches) if len(batches[i]) < max_files_per_batch]
        # candidate_indices can't be empty: num_batches was sized so that
        # num_batches * max_files_per_batch >= total_files.
        chosen = min(candidate_indices, key=lambda i: batch_weights[i])
        batches[chosen].append(path)
        batch_weights[chosen] += est_mb

    # Drop any batch that ended up empty (only possible if num_batches
    # was sized generously and the file count didn't fill every slot --
    # shouldn't normally happen given the ceil-division sizing, but
    # guarded defensively rather than assumed away).
    non_empty = [(b, w) for b, w in zip(batches, batch_weights) if b]
    if not non_empty:
        return [], []
    batches, batch_weights = zip(*non_empty)
    return list(batches), list(batch_weights)
