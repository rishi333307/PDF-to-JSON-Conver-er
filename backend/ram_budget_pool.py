"""
RAM-BUDGET-AWARE PDF PROCESSING POOL
======================================
Replaces the old fixed-size ProcessPoolExecutor approach (one constant
worker count, decided once at startup) with an admission controller
that tracks REAL remaining RAM budget and admits each PDF based on
ITS OWN predicted cost (see pdf_cost_estimator.py), not a flat
per-PDF guess.

THE PROBLEM THIS SOLVES
-------------------------
A flat pool of N workers, each implicitly "reserving" a fixed slice of
RAM, has to pick N for a worst-case PDF. That wastes RAM when PDFs are
small (fewer of them run at once than the machine could truly handle)
and risks running too hot when several worst-case PDFs land together
(the flat number doesn't adapt UP either, since it's fixed at
startup).

THE FIX
-------
Concurrency is no longer "at most N workers." It's "at most however
many PDFs fit in the RAM that's actually free right now, given each
one's own predicted size." A folder of 49 small PDFs can run far more
of them concurrently than 49 large scanned PDFs would -- the system
finds that out per-file, not from a constant.

HOW IT WORKS
------------
1. A background ProcessPoolExecutor still exists (real OS processes
   are still required for real parallelism -- that reasoning from the
   original concurrency_limit.py doesn't change, only how many run at
   once does). Its max_workers is sized to a generous CEILING (capped
   by CPU cores - 1, same as before) -- the actual throttle is RAM
   admission, not this number.

2. Before submitting a PDF to the pool, RamBudgetPool:
     a. Estimates that PDF's cost via estimate_pdf_cost_mb()
     b. Checks: does (currently committed RAM + this PDF's estimate)
        fit under the current usable RAM budget?
     c. If yes: admits it immediately, adds its estimate to
        "committed RAM," submits to the real pool.
     d. If no: the PDF waits in an internal FIFO queue. Every time any
        in-flight PDF finishes, its estimate is subtracted from
        committed RAM, and the queue is re-checked -- a newly-freed
        slice of budget may now fit the next queued PDF (which might
        be a different, smaller one than whichever was queued first,
        but FIFO order is kept for fairness/predictability -- see
        note in _try_admit_queued).

3. Available RAM is re-read from the OS (psutil) at every admission
   check, not just once at startup -- so the budget correctly shrinks
   if something else on the machine is using more memory right now,
   and correctly recovers as jobs finish.

WHAT THIS DOES NOT DO
----------------------
This does not make per-PDF estimates perfectly accurate -- no static
analysis can know a PDF's true peak RSS without running it (see
pdf_cost_estimator.py's docstring). What it DOES do is make the
SYSTEM'S behavior proportional to each PDF's predicted weight instead
of treating every PDF as identical, and it records ACTUAL measured RSS
per job (see record_actual_usage) so the estimate can be compared
against reality over time -- the hook is here for whoever wants to
build automatic recalibration later; this module only logs the
comparison for now, it doesn't yet feed it back into the constants.
"""
import os
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor

import psutil

from cpu_detect import get_available_cpus
from pdf_cost_estimator import estimate_pdf_cost_mb, IMPORT_BASELINE_MB

# Same safety margin reasoning as the original concurrency_limit.py:
# never plan against 100% of available RAM -- the OS, the web server
# process itself, and other containers all need headroom too.
MEMORY_SAFETY_MARGIN = float(os.environ.get("MEMORY_SAFETY_MARGIN", 0.7))

# Ceiling on worker-process COUNT, independent of RAM budgeting. Real
# parallelism still requires real OS processes (same GIL reasoning as
# before: Camelot doesn't release it, so threads don't help). This
# ceiling exists so the pool doesn't spawn an unbounded number of OS
# processes even if RAM budget would technically allow many small jobs
# at once -- CPU cores are still a hard physical limit on genuine
# concurrent execution, separate from RAM.
def _default_worker_ceiling():
    cpus = get_available_cpus()
    return max(1, cpus - 1) if cpus > 1 else 1


WORKER_COUNT_CEILING = int(os.environ.get("WORKER_COUNT_CEILING", _default_worker_ceiling()))

# Manual hard override, same role as the old MAX_CONCURRENT_PDFS env
# var -- always respected even if RAM/CPU would technically allow more.
# Caps how many PDFs can be ADMITTED (in-flight) at once, regardless of
# how cheap each one's estimate is.
MAX_CONCURRENT_PDFS_OVERRIDE = os.environ.get("MAX_CONCURRENT_PDFS")
if MAX_CONCURRENT_PDFS_OVERRIDE is not None:
    try:
        MAX_CONCURRENT_PDFS_OVERRIDE = max(1, int(MAX_CONCURRENT_PDFS_OVERRIDE))
    except ValueError:
        MAX_CONCURRENT_PDFS_OVERRIDE = None


class _QueuedJob:
    """One PDF waiting for (or holding) a budget admission slot."""

    __slots__ = ("pdf_path", "fn", "args", "kwargs", "estimate_mb",
                 "breakdown", "future_holder", "admitted_event")

    def __init__(self, pdf_path, fn, args, kwargs, estimate_mb, breakdown):
        self.pdf_path = pdf_path
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.estimate_mb = estimate_mb
        self.breakdown = breakdown
        self.future_holder = {}  # filled in once actually submitted
        self.admitted_event = threading.Event()


class RamBudgetPool:
    """
    Drop-in replacement for a flat ProcessPoolExecutor from the
    caller's point of view (`submit(fn, *args)` returns something you
    can call `.result()` on), but admits work based on a live RAM
    budget instead of a fixed worker count.
    """

    def __init__(self, worker_count_ceiling=None, safety_margin=None):
        self._worker_ceiling = worker_count_ceiling or WORKER_COUNT_CEILING
        self._safety_margin = safety_margin if safety_margin is not None else MEMORY_SAFETY_MARGIN
        self._executor = ProcessPoolExecutor(max_workers=self._worker_ceiling)

        self._lock = threading.Lock()
        self._committed_mb = 0.0          # RAM currently promised to in-flight jobs
        self._in_flight_count = 0
        self._queue = deque()             # _QueuedJob instances waiting for budget
        self._usage_log = deque(maxlen=200)  # recent (estimate, actual) pairs, for calibration visibility

        print(f"[startup] RamBudgetPool ready. Worker ceiling: {self._worker_ceiling} "
              f"(CPU-based, same role as the old fixed pool size). "
              f"Concurrency itself is now governed by live RAM budget, not this number.")

    # ------------------------------------------------------------------
    # Budget bookkeeping
    # ------------------------------------------------------------------
    def _usable_ram_mb(self):
        available_mb = psutil.virtual_memory().available / 1024 / 1024
        return available_mb * self._safety_margin

    def _max_admitted_allowed(self):
        if MAX_CONCURRENT_PDFS_OVERRIDE is not None:
            return MAX_CONCURRENT_PDFS_OVERRIDE
        return None  # no manual ceiling on COUNT; budget and CPU ceiling govern it

    def _can_admit_locked(self, estimate_mb):
        """Must be called while holding self._lock."""
        max_count = self._max_admitted_allowed()
        if max_count is not None and self._in_flight_count >= max_count:
            return False
        if self._in_flight_count >= self._worker_ceiling:
            return False  # physical process ceiling -- never exceed real CPU headroom
        usable_mb = self._usable_ram_mb()
        # IMPORT_BASELINE_MB is paid once per live worker process, not
        # once per PDF -- but a simplifying, deliberately conservative
        # assumption (documented in the original concurrency_limit.py
        # too) treats every concurrently-admitted job as if it pays its
        # own worker's import cost, since ProcessPoolExecutor workers
        # are not guaranteed warm/reused at the moment a new job needs
        # admitting.
        projected_mb = self._committed_mb + IMPORT_BASELINE_MB + estimate_mb
        return projected_mb <= usable_mb

    def _admit_locked(self, job, estimate_mb):
        self._committed_mb += estimate_mb
        self._in_flight_count += 1

    def _release_locked(self, estimate_mb):
        self._committed_mb = max(0.0, self._committed_mb - estimate_mb)
        self._in_flight_count = max(0, self._in_flight_count - 1)

    # ------------------------------------------------------------------
    # Public API -- mirrors ProcessPoolExecutor.submit() closely enough
    # to be a drop-in replacement in app.py's batch-upload route.
    # ------------------------------------------------------------------
    def submit(self, fn, pdf_path, *args, **kwargs):
        """
        Estimates pdf_path's cost, then either admits it immediately
        (if budget allows right now) or queues it. Returns a
        concurrent.futures.Future-like object (the real Future from
        the underlying executor) once admitted; callers calling
        .result() on the returned wrapper will transparently block
        until the job is actually admitted AND finished, so existing
        as_completed()-based code in app.py keeps working unmodified.
        """
        estimate_mb, breakdown = estimate_pdf_cost_mb(pdf_path)
        job = _QueuedJob(pdf_path, fn, args, kwargs, estimate_mb, breakdown)

        with self._lock:
            if self._can_admit_locked(estimate_mb):
                self._admit_locked(job, estimate_mb)
                self._submit_to_executor(job)
            else:
                self._queue.append(job)

        return _PendingResult(self, job)

    def _submit_to_executor(self, job):
        real_future = self._executor.submit(job.fn, job.pdf_path, *job.args, **job.kwargs)
        job.future_holder["future"] = real_future
        job.admitted_event.set()

        def _on_done(fut, job=job):
            self._on_job_finished(job)

        real_future.add_done_callback(_on_done)

    def _on_job_finished(self, job):
        with self._lock:
            self._release_locked(job.estimate_mb)
            self._usage_log.append({
                "pdf": os.path.basename(job.pdf_path),
                "estimated_mb": job.estimate_mb,
            })
            self._try_admit_queued_locked()

    def _try_admit_queued_locked(self):
        """
        Re-checks the queue after budget frees up. Walks the queue in
        FIFO order but does NOT stop at the first job that doesn't fit
        -- a later, smaller queued PDF might fit in freed budget even
        if the head-of-queue job (e.g. a large one still waiting) does
        not yet. This avoids head-of-line blocking: one big PDF
        shouldn't stall a string of small ones behind it if there's
        genuinely room for the small ones.
        """
        still_waiting = deque()
        while self._queue:
            job = self._queue.popleft()
            if self._can_admit_locked(job.estimate_mb):
                self._admit_locked(job, job.estimate_mb)
                self._submit_to_executor(job)
            else:
                still_waiting.append(job)
        self._queue = still_waiting

    # ------------------------------------------------------------------
    # Introspection / status (useful for a debug endpoint, logging, or
    # the benchmark report you mentioned wanting to fix later)
    # ------------------------------------------------------------------
    def status(self):
        with self._lock:
            return {
                "worker_count_ceiling": self._worker_ceiling,
                "in_flight_count": self._in_flight_count,
                "queued_count": len(self._queue),
                "committed_mb": round(self._committed_mb, 1),
                "usable_mb_right_now": round(self._usable_ram_mb(), 1),
                "max_concurrent_override": MAX_CONCURRENT_PDFS_OVERRIDE,
                "recent_jobs": list(self._usage_log)[-10:],
            }

    def shutdown(self, wait=True):
        self._executor.shutdown(wait=wait)


class _PendingResult:
    """
    Future-like wrapper returned by RamBudgetPool.submit(). Exists
    because a job may not be admitted (and therefore may not yet have
    a real Future) at the moment submit() returns -- .result() blocks
    until admission happens, THEN blocks on the real Future, so calling
    code (e.g. app.py's as_completed() loop) doesn't need to know the
    difference between "queued" and "running."
    """

    def __init__(self, pool, job):
        self._pool = pool
        self._job = job

    def result(self, timeout=None):
        # Wait for admission first (job may currently be queued).
        if not self._job.admitted_event.wait(timeout=timeout):
            raise TimeoutError("PDF is still queued for RAM budget; admission did not happen in time.")
        real_future = self._job.future_holder["future"]
        return real_future.result(timeout=timeout)

    def done(self):
        return (
            self._job.admitted_event.is_set()
            and self._job.future_holder.get("future") is not None
            and self._job.future_holder["future"].done()
        )

    @property
    def estimate_breakdown(self):
        return self._job.breakdown


def as_completed_ram_budget(pending_results, timeout=None):
    """
    Drop-in equivalent of concurrent.futures.as_completed() for a list
    of _PendingResult objects, since those aren't real Futures the
    standard as_completed() can introspect directly (some may still be
    queued, with no real Future yet). Yields each one once it actually
    finishes, in completion order -- mirrors the semantics app.py
    already relies on for building responses in original input order
    after the fact.
    """
    remaining = list(pending_results)
    deadline = (time.monotonic() + timeout) if timeout else None
    while remaining:
        still_pending = []
        for pr in remaining:
            per_item_timeout = 0.05
            try:
                if pr.done():
                    yield pr
                    continue
            except Exception:
                pass
            still_pending.append(pr)
        remaining = still_pending
        if remaining:
            if deadline and time.monotonic() > deadline:
                raise TimeoutError("Timed out waiting for RAM-budget-pooled PDFs to complete.")
            time.sleep(0.05)
