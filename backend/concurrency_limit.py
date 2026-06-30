"""
Decides how many PDFs to process in parallel, safely, on whatever machine
this happens to run on -- your 4-core laptop, someone else's 8-core
machine, or a tiny Render free-tier box.

Grounded in REAL measurements taken from this project's own code, not
guesses:
  - Each worker process pays a one-time ~150MB import cost (camelot
    pulls in OpenCV). This is paid ONCE per worker process, not once
    per PDF.
  - Each PDF processed adds roughly 150-300MB of its own, depending on
    page count and table complexity (measured: a 9-page real-world PDF
    peaked at ~310MB total, i.e. ~160MB above the import baseline).
  - This is intentionally a conservative per-PDF estimate. Larger or
    more complex PDFs (more pages, more tables, scanned pages needing
    OCR) will cost more -- this module exists specifically so the limit
    adapts to the machine rather than being hardcoded.

The final concurrency limit is the MINIMUM of:
  1. What memory allows  (available RAM / per-worker cost)
  2. What CPU allows      (available cores, leaving 1 free for the
                            web server itself to stay responsive)
  3. A hard ceiling        (MAX_CONCURRENT_PDFS env var, so you can
                            always clamp it down manually regardless
                            of what the machine reports)
"""
import os
import psutil
from cpu_detect import get_available_cpus

# Measured baseline: cost of importing camelot/cv2/etc in a fresh worker,
# before any PDF-specific work happens. Paid once per worker PROCESS.
IMPORT_BASELINE_MB = 180  # rounded up from measured ~145MB, safety margin

# Measured marginal cost: how much MORE memory one typical PDF adds on
# top of the import baseline. This is a conservative per-PDF estimate;
# very large/complex PDFs will exceed it, which is what the safety
# margin below is for.
PER_PDF_MARGINAL_MB = 250  # rounded up from measured ~160MB on real PDF

# Extra safety margin so we never plan to use literally 100% of RAM
# (the OS, the web server process, other containers, etc. need room too)
MEMORY_SAFETY_MARGIN = 0.7  # only plan against 70% of available RAM


def get_max_concurrent_pdfs() -> int:
    """
    Returns a safe number of PDFs to process simultaneously, computed
    fresh each time this is called (so it reacts correctly if available
    memory changes at runtime, e.g. other processes using more RAM).
    """
    # Hard ceiling from environment variable, if set. Always respected,
    # even if the machine could technically handle more -- this is the
    # manual override / safety valve.
    env_ceiling = os.environ.get("MAX_CONCURRENT_PDFS")
    if env_ceiling is not None:
        try:
            env_ceiling = max(1, int(env_ceiling))
        except ValueError:
            env_ceiling = None

    # Memory-based limit
    available_mb = psutil.virtual_memory().available / 1024 / 1024
    usable_mb = available_mb * MEMORY_SAFETY_MARGIN
    cost_per_worker_first_pdf = IMPORT_BASELINE_MB + PER_PDF_MARGINAL_MB
    # First worker pays import cost; this is a simplification that
    # treats every worker as paying its own import cost (true for
    # process-based workers, which is what we're using -- see writeup).
    memory_limit = max(1, int(usable_mb // cost_per_worker_first_pdf))

    # CPU-based limit: leave 1 core free for the web server / event loop
    # itself, so the app stays responsive while PDFs are crunching
    available_cpus = get_available_cpus()
    cpu_limit = max(1, available_cpus - 1) if available_cpus > 1 else 1

    limit = min(memory_limit, cpu_limit)
    if env_ceiling is not None:
        limit = min(limit, env_ceiling)

    return max(1, limit)


def explain() -> dict:
    """Returns the full breakdown, for logging/debugging/the status endpoint."""
    available_mb = psutil.virtual_memory().available / 1024 / 1024
    usable_mb = available_mb * MEMORY_SAFETY_MARGIN
    cost_per_worker = IMPORT_BASELINE_MB + PER_PDF_MARGINAL_MB
    memory_limit = max(1, int(usable_mb // cost_per_worker))
    available_cpus = get_available_cpus()
    cpu_limit = max(1, available_cpus - 1) if available_cpus > 1 else 1
    env_ceiling = os.environ.get("MAX_CONCURRENT_PDFS")

    return {
        "available_ram_mb": round(available_mb, 1),
        "usable_ram_mb_after_safety_margin": round(usable_mb, 1),
        "estimated_cost_per_worker_mb": cost_per_worker,
        "memory_based_limit": memory_limit,
        "available_cpus": available_cpus,
        "cpu_based_limit": cpu_limit,
        "env_ceiling": env_ceiling,
        "final_max_concurrent_pdfs": get_max_concurrent_pdfs(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(explain(), indent=2))
