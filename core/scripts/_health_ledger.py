"""_health_ledger.py — shared read helpers for the per-agent health ledger.

Single source of truth for ledger I/O (`agents/<agent>/health/<YYYY-MM-DD>.jsonl`)
across the Phase 2+ consumers (health-regression-check, health-ledger-query).
Spec: core/config/conventions/health-ledger.md.

NOTE: health-ledger-append.py predates this module and keeps its own inline
`_recent_records` reader (it was committed + tested in Phase 1). Consolidate it
onto `recent_records` here when next touching that script — deferred now to
avoid destabilizing the working append path.

All readers are tolerant/fail-open: a missing dir, unreadable file, or corrupt
line yields empty/None, never an exception (telemetry must never break a caller).
"""
from __future__ import annotations

import json
from pathlib import Path


def lsq_slope(ys):
    """Least-squares slope over y-values at x = 0..n-1 (oldest->newest). Mirrors
    health-ledger-append.py's `_lsq_slope` (that script predates this module and
    keeps its own copy — see module docstring)."""
    n = len(ys)
    if n < 2:
        return 0.0
    sx = sum(range(n))
    sy = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))
    sxx = sum(i * i for i in range(n))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    return (n * sxy - sx * sy) / denom


def recent_records(health_dir, limit):
    """Up to `limit` most recent ledger records (newest-first) by walking the
    daily-rotated day-files newest-date-first and reversing within each file
    (records are append-ordered oldest-first)."""
    out = []
    health_dir = Path(health_dir)
    try:
        if not health_dir.exists():
            return out
        day_files = sorted(
            (p for p in health_dir.iterdir()
             if p.is_file() and p.suffix == ".jsonl"),
            key=lambda p: p.name, reverse=True)
    except OSError:
        return out
    for f in day_files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                return out
    return out


def latest_nonwarmup(records):
    """The newest non-warmup record with a non-null composite, or None.
    `records` must be newest-first (as returned by recent_records)."""
    for r in records:
        if not r.get("warmup") and r.get("composite") is not None:
            return r
    return None


def consecutive_below_baseline(records):
    """Count of consecutive `below_baseline` records from the newest end,
    skipping warmup/null records, stopping at the first non-below record.
    `records` newest-first. Warmup/null records neither count nor break the
    streak (they are excluded from detection per spec §7)."""
    n = 0
    for r in records:
        if r.get("warmup") or r.get("composite") is None:
            continue
        if r.get("below_baseline"):
            n += 1
        else:
            break
    return n


def calibration_progress(records):
    """(num_days, num_records) for the calibration AND-gate (spec §10), over a
    bounded recent-window list. num_days = distinct calendar dates present in
    record timestamps; num_records = total records read. `records`
    order-agnostic.

    WARNING: when the caller passes a bounded window (e.g. recent_records(d, 120)),
    num_days under-counts for an agent that records many iterations per day — 120
    records can span < 30 calendar days, so the 30-day gate would never trip.
    For the calibration AND-gate use `full_calibration_progress` (scans the whole
    ledger). This window form remains for cheap progress display."""
    dates = set()
    count = 0
    for r in records:
        ts = r.get("ts")
        if isinstance(ts, str) and len(ts) >= 10:
            dates.add(ts[:10])
        count += 1
    return len(dates), count


def full_calibration_progress(health_dir):
    """(num_days, num_records) across the ENTIRE ledger history — distinct
    calendar dates from record timestamps and total record count, scanning every
    day-file. This is the CORRECT input for the 30-day calibration AND-gate
    (spec §10): unlike `calibration_progress` over a bounded recent window, it
    cannot under-count days when an agent records many iterations per calendar
    day. Scanned each sweep only until the gate first trips (the caller writes a
    `.calibrated` marker and short-circuits thereafter — the gate is monotonic on
    an append-only ledger). Tolerant/fail-open: missing dir or unreadable file
    yields (0, 0)."""
    health_dir = Path(health_dir)
    dates = set()
    records = 0
    try:
        if not health_dir.exists():
            return 0, 0
        day_files = [p for p in health_dir.iterdir()
                     if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return 0, 0
    for f in day_files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            records += 1
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            ts = rec.get("ts")
            if isinstance(ts, str) and len(ts) >= 10:
                dates.add(ts[:10])
    return len(dates), records


def calibration_state(health_dir, min_days, min_records):
    """THE definition of "is this agent calibrated?" (spec §10). Returns
    (calibrated, progress) where progress is (days, records) when a full scan
    ran, or None when the `.calibrated` marker short-circuited it.

    Both halves of the subsystem MUST route their calibration read through this
    helper — `health-regression-check.py` (detection) and `health-revert.py`
    (revert, whose `route_candidate` calls `mode == full AND calibrated` the
    master safety gate). They previously each computed `calibrated` themselves
    and DRIFTED (g-115-3125): the revert half used `calibration_progress(
    recent_records(d, 200))`, precisely the bounded-window form the docstring
    above and spec §10 say MUST NOT gate calibration. Because a busy agent packs
    200 records into a handful of calendar days, that half read False on every
    PRODUCTIVE agent — the more health evidence an agent generated, the more
    permanently its revert authority stayed locked — while the detection half
    read True. Measured on zeta 2026-07-26: windowed (6 days/200 records) =>
    False; full-history (43 days/1150 records) => True.

    Read-only by design. The one-time marker WRITE stays with the detection
    half, which owns the fires-exactly-once `calibration_just_completed` edge;
    duplicating the write here would make that edge fire twice.
    """
    if (Path(health_dir) / ".calibrated").exists():
        return True, None
    days, records = full_calibration_progress(health_dir)
    return (days >= min_days and records >= min_records), (days, records)
