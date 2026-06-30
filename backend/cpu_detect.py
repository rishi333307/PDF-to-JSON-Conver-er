"""
Robust "how many CPUs can I actually use right now" detector.

Why this exists (proven by testing, not assumed):
  - os.cpu_count() reports the HOST's total cores, and ignores Docker's
    --cpus and --cpuset-cpus limits. Confirmed by the Python/Docker
    community (see: bugs.python.org/issue36054, multiple GitHub issues
    from joblib, sentry, pre-commit, tifffile, nginx).
  - os.sched_getaffinity(0) correctly reflects --cpuset-cpus (CPU pinning),
    but NOT --cpus (a CFS bandwidth quota, a different Linux mechanism).
  - So a correct detector must check BOTH: affinity AND the cgroup CFS
    quota files, and take the minimum -- exactly what joblib's internal
    _cpu_count_user() does (verified via GitHub issue joblib/joblib#1206).

This means the SAME Dockerfile, unmodified, will correctly detect:
  - 4 cores on a 4-core laptop (no Docker limit set)
  - 8 cores on someone else's 8-core machine (no Docker limit set)
  - 2 cores if anyone runs it with `docker run --cpus=2 ...`
  - 0.1 effective core (rounds to 1) on Render's free tier
with zero code changes -- the detection happens at runtime, every time.
"""
import os
import math


def get_available_cpus() -> int:
    """
    Returns the number of CPUs this process can actually use right now,
    correctly respecting Docker/cgroup limits if present. Always returns
    at least 1.
    """
    # Step 1: affinity-based count (host cores, or fewer if --cpuset-cpus
    # pinned this container to a subset of cores)
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity_count = len(os.sched_getaffinity(0))
        except (NotImplementedError, OSError):
            affinity_count = os.cpu_count() or 1
    else:
        # sched_getaffinity doesn't exist on macOS/Windows -- fall back
        affinity_count = os.cpu_count() or 1

    # Step 2: cgroup v2 quota check (modern Docker, single unified file)
    cgroup_v2_count = _read_cgroup_v2_quota()
    if cgroup_v2_count is not None:
        return max(1, min(affinity_count, cgroup_v2_count))

    # Step 3: cgroup v1 quota check (older Docker / older host kernels)
    cgroup_v1_count = _read_cgroup_v1_quota()
    if cgroup_v1_count is not None:
        return max(1, min(affinity_count, cgroup_v1_count))

    # No cgroup quota file found at all (not in a container, or a
    # platform that doesn't expose cgroups) -- affinity is our best signal
    return max(1, affinity_count)


def _read_cgroup_v2_quota():
    """
    cgroup v2 exposes a single file: /sys/fs/cgroup/cpu.max
    Format: "<quota> <period>" in microseconds, or "max <period>" if
    there's no limit set.
    """
    path = "/sys/fs/cgroup/cpu.max"
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            content = f.read().strip()
        quota_str, period_str = content.split()
        if quota_str == "max":
            return None  # no limit set
        quota = int(quota_str)
        period = int(period_str)
        if quota <= 0 or period <= 0:
            return None
        return max(1, math.ceil(quota / period))
    except (ValueError, OSError):
        return None


def _read_cgroup_v1_quota():
    """
    cgroup v1 exposes two separate files (the legacy mechanism, still
    common -- this is exactly what joblib checks for):
      /sys/fs/cgroup/cpu/cpu.cfs_quota_us  (e.g. 200000 = 2 cores worth)
      /sys/fs/cgroup/cpu/cpu.cfs_period_us (e.g. 100000 = standard period)
    A quota of -1 means "no limit set".
    """
    quota_path = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
    period_path = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
    if not (os.path.exists(quota_path) and os.path.exists(period_path)):
        return None
    try:
        with open(quota_path, "r") as f:
            quota = int(f.read().strip())
        with open(period_path, "r") as f:
            period = int(f.read().strip())
        if quota <= 0 or period <= 0:
            return None  # -1 or unset means no limit
        return max(1, math.ceil(quota / period))
    except (ValueError, OSError):
        return None


if __name__ == "__main__":
    # Quick self-test / diagnostic when run directly
    print("Affinity count:", len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else "N/A (no sched_getaffinity)")
    print("os.cpu_count() (host total, ignores Docker limits):", os.cpu_count())
    print("cgroup v2 quota-derived count:", _read_cgroup_v2_quota())
    print("cgroup v1 quota-derived count:", _read_cgroup_v1_quota())
    print("FINAL get_available_cpus():", get_available_cpus())
