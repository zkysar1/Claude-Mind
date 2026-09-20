"""Per-file lock manager for the daemon's write path.

Two-layer locking:

  Layer 1 — `threading.Lock` (in-process, fast):
    One Lock per canonical absolute path. Two daemon threads writing to the
    same JSONL file serialise here without ever touching the filesystem.
    Cost: ~1 microsecond per acquire on contention.

  Layer 2 — `<path>.lock` file via _fileops.acquire_lock (cross-process):
    Atomic O_CREAT|O_EXCL on a sibling .lock file. The fallback-to-direct-
    python path uses this EXACT primitive, so daemon and fallback serialise
    correctly. Cost: ~1-10 ms per acquire on OneDrive.

CRITICAL: do not "simplify" by dropping the file lock. The threading.Lock
is an OPTIMISATION; the file lock is the SAFETY mechanism that lets the
daemon and fallback path coexist without corrupting JSONL.

Usage:
    with file_locks.locked(path):
        # path.with_suffix('.lock') is held; threading.Lock for path is held
        # do the read-modify-write
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "core" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# Imported once. acquire_lock/release_lock take explicit paths — they do NOT
# reference _paths globals (WORLD_DIR/META_DIR), so importing _fileops here
# is daemon-safe despite _fileops' top-level `from _paths import WORLD_DIR`.
# _rmw_with_conflict_retry is the own-cloud optimistic-concurrency retry wrapper
# (#38) — it calls get_backend().conflict_error internally, so it stays a
# transparent single pass on LocalBackend.
from _fileops import (  # noqa: E402
    acquire_lock,
    release_lock,
    _rmw_with_conflict_retry,
)


class FileLockManager:
    """Manages per-path threading.Lock objects. Thread-safe."""

    def __init__(self) -> None:
        self._locks: Dict[str, threading.Lock] = {}
        self._table_lock = threading.Lock()

    def get(self, path: Path, key: Optional[str] = None) -> threading.Lock:
        """Return the threading.Lock for `path`, creating it if absent.

        Keyed by the canonical absolute path string — different aliases for
        the same file (relative paths, symlinks) MUST resolve to the same
        key, else two threads could write through different Lock objects.

        `key` lets a caller that has ALREADY resolved the path hand the key
        in rather than pay a second `Path.resolve()` — that syscall, not the
        dict work, is what a duplicate costs (measured: resolving twice per
        acquire added 0.0115ms, 18.4% of a local-backend acquire/release).
        It MUST be `str(Path(path).resolve())`; passing anything else splits
        one file across two Lock objects and silently removes the
        serialisation this class exists to provide.
        """
        key = str(Path(path).resolve()) if key is None else key
        # Fast path: hit-without-lock is safe because dict-get of an existing
        # key is atomic in CPython and we never delete entries.
        cached = self._locks.get(key)
        if cached is not None:
            return cached
        with self._table_lock:
            cached = self._locks.get(key)
            if cached is None:
                cached = threading.Lock()
                self._locks[key] = cached
            return cached


_GLOBAL_MANAGER = FileLockManager()


def manager() -> FileLockManager:
    return _GLOBAL_MANAGER


# ─── Write-path hold telemetry () ─────────────────────────────────
# WHY THIS EXISTS: `thread_lock.acquire()` in locked() below is UNBOUNDED. A
# holder that never releases parks every other writer in an indefinite futex
# wait, and NOTHING reported it — GET /v1/admin/health answered 200 in 0.2ms
# throughout a total box-wide WM-write freeze, so mind-api-start.sh's
# idempotent "alive and responsive → exit 0" fast path repaired nothing. The
# daemon was liveness-green and write-dead at the same instant, and the
# liveness probe is structurally incapable of telling the difference because
# it never touches the write path.
#
# These two dicts ARE that missing surface. They record what the lock layer
# already knows and previously threw away: when each hold began, and how many
# threads are blocked behind it.
#
# COST AND WHY IT IS LEGAL ON /health: reading this is process memory only —
# no filesystem, no lock acquisition, and NO STORE ROUND TRIP. That is what
# keeps it compatible with ready.py's standing rule ("DO NOT add store checks
# to /health") and leaves rt_ensure_running's hot path unchanged: it still
# makes exactly one request and that request still touches no store.
#
# NOT a second lock-ordering participant: _TELEMETRY_LOCK is only ever held
# for a few dict ops and is NEVER held across thread_lock.acquire(), the file
# lock, or the yield — so it cannot join a deadlock cycle.
_TELEMETRY_LOCK = threading.Lock()
_HOLDS: Dict[str, float] = {}     # lock key → monotonic time the hold began
_WAITING: Dict[str, int] = {}     # lock key → threads blocked on acquire

# A hold older than this is pathological BY CONSTRUCTION, not by guesswork:
# acquire_lock's own file-lock timeout is 10s (locked()'s `timeout` default)
# and _write_queue steals a turn after 30s (`hold_stale`), so no legitimate
# hold survives a minute. Env-overridable so a test can drive the threshold
# instead of sleeping past it.
WEDGE_SECONDS = float(os.environ.get("MIND_WRITE_PATH_WEDGE_S", "60") or 60)

# ─── Bounded acquire () ───────────────────────────────────────────
#  made the wedge OBSERVABLE (the two dicts above) and deliberately
# left the acquire unbounded — the comment there still says so. Observability
# was the right first step and it is not the fix: on cc-04 a wm-prune request
# timed out client-side at RT_CURL_TIMEOUT=90 and from that moment EVERY WM
# write hung forever while reads returned in 1s through the same daemon
# (guard-6895). THE CLIENT GIVING UP DOES NOT RELEASE A SERVER-SIDE LOCK, and
# an unbounded acquire turns one stuck holder into a permanent box-wide write
# death that only `mind-api-start.sh --restart` clears.
#
# WHY A GIVE-UP AND NOT A SHORTER HOLD: the holder is parked in I/O (a
# multi-megabyte own-cloud PUT). Nothing in this process can shorten or
# interrupt it. What this bound fixes is the PILE-UP behind it — waiters stop
# accumulating in an indefinite futex wait and start failing with a diagnosis
# that names the holder, so the box is debuggable instead of silently dead.
#
# WHY 240s AND NOT WEDGE_SECONDS (60): these two thresholds answer different
# questions and the asymmetry is deliberate. Reporting `wedged: true` on
# /health is advisory and cheap to get wrong. REFUSING A WRITE is not, and a
# legitimate hold here is a whole-file rewrite whose duration is set by object
# size and network, not by this module — alpha's measured bounds on the real
# incident were 75s and 240s, which establish "longer than 240s", never "no
# legitimate write takes 60s". So the give-up sits well past every bound that
# incident tried. Raising WEDGE_SECONDS must NOT silently raise this.
#
# THE SAFETY PROPERTY THAT MAKES THIS SHIPPABLE ON A 111-CALL-SITE WRITE PATH:
# we give up ONLY when the CURRENT holder has itself already held for longer
# than the give-up threshold. In every case where behaviour changes, the
# pre-existing behaviour was an unbounded hang. A young holder — however slow —
# is waited on exactly as before.
ACQUIRE_GIVEUP_SECONDS = float(
    os.environ.get("MIND_WRITE_PATH_GIVEUP_S", "240") or 240)
# How often a blocked waiter re-checks the holder's age. Not a timeout: a
# failed poll loops. Small enough to react promptly, large enough that a
# hot lock does not spin.
ACQUIRE_POLL_SECONDS = float(
    os.environ.get("MIND_WRITE_PATH_POLL_S", "5") or 5)


class WritePathWedged(TimeoutError):
    """A writer gave up because the CURRENT holder is wedged.

    Subclasses TimeoutError ON PURPOSE: callers that already map a lock
    TimeoutError to a 503 "lock busy; try again" (see
    meta/skill_quality_score.py) then degrade to that same accurate answer
    instead of a 500, with no edit at the call site. A new exception type
    hanging off Exception would have turned 111 call sites into 500s.
    """


def _holder_age(key: str) -> Optional[float]:
    """Seconds the current hold on `key` has been held, or None if unheld.

    None is the honest answer for "nobody holds it": the holder released
    between our failed acquire and this read, which means the next poll will
    take the lock. Callers MUST treat None as "keep waiting", never as
    "wedged" — the fail-safe direction is to wait, because refusing a write
    that could have succeeded is the expensive error here.
    """
    with _TELEMETRY_LOCK:
        started = _HOLDS.get(key)
    return None if started is None else time.monotonic() - started


def acquire_or_wedge(thread_lock: threading.Lock, key: str, *,
                     giveup_seconds: Optional[float] = None,
                     poll_seconds: Optional[float] = None) -> None:
    """Acquire `thread_lock`, giving up only if the holder is itself wedged.

    Blocks like a bare `acquire()` for any holder younger than the give-up
    threshold. Raises `WritePathWedged` — never returns False — so a caller
    that forgets to check a boolean cannot proceed unlocked.
    """
    giveup_seconds = (ACQUIRE_GIVEUP_SECONDS if giveup_seconds is None
                      else giveup_seconds)
    poll_seconds = (ACQUIRE_POLL_SECONDS if poll_seconds is None
                    else poll_seconds)
    while True:
        if thread_lock.acquire(timeout=poll_seconds):
            return
        age = _holder_age(key)
        if age is not None and age > giveup_seconds:
            with _TELEMETRY_LOCK:
                blocked = _WAITING.get(key, 0)
            raise WritePathWedged(
                f"write path wedged: {key} has been held for {age:.0f}s "
                f"(give-up {giveup_seconds:.0f}s), {blocked} writer(s) "
                f"blocked. The holder is parked in I/O and cannot be "
                f"interrupted from here; this request is refused rather "
                f"than parked behind it. Recover with "
                f"`mind-api-start.sh --restart` (guard-6895) — note it is "
                f"--restart, not --recycle. GET /v1/admin/health reports "
                f"the live verdict under write_path.")


def write_path_status(wedge_seconds: Optional[float] = None) -> dict:
    """Verdict on whether THIS process's write path is wedged.

    Pure in-process read (see the cost note above). `wedged` is the field a
    consumer branches on; the rest is diagnosis for whoever reads the wedge.

    `wedged` is False when no hold is in flight — an idle daemon is not a
    wedged one, and the fail-safe direction matters here: a false `wedged`
    would restart a daemon that is serving the whole box.
    """
    wedge_seconds = WEDGE_SECONDS if wedge_seconds is None else wedge_seconds
    now = time.monotonic()
    with _TELEMETRY_LOCK:
        holds = list(_HOLDS.items())
        blocked = sum(_WAITING.values())
    longest = 0.0
    longest_path = None
    for key, started in holds:
        age = now - started
        if age > longest:
            longest, longest_path = age, key
    return {
        "wedged": longest > wedge_seconds,
        "longest_hold_s": round(longest, 3),
        "longest_hold_path": longest_path,
        "holds_in_flight": len(holds),
        "blocked_writers": blocked,
        "wedge_threshold_s": wedge_seconds,
    }


@contextlib.contextmanager
def thread_locked(path: Path, *, key: Optional[str] = None,
                  giveup_seconds: Optional[float] = None,
                  poll_seconds: Optional[float] = None):
    """Hold ONLY the per-path threading lock — bounded and instrumented.

    This is the Layer-1 half of `locked()` with the Layer-2 file lock left
    out, for the one caller that needs a NON-STANDARD file-lock path and so
    cannot use `locked()` at all (meta/skill_quality_score.py takes
    `skill-quality.yaml.lock`, not the `.lock` convention `locked()` hardcodes).

    WHY THIS EXISTS AS A SHARED HELPER RATHER THAN A SECOND INLINE COPY
    (g-115-10378): that caller previously did a bare `thread_lock.acquire()`,
    which was unbounded (the defect g-115-10161 fixed in `locked()`) AND
    unregistered — it wrote no `_HOLDS` entry, so `write_path_status()` could
    not see a wedge on that path at all and /health would report a clean
    verdict over a wedged write path. The bounding and the registration are
    the same few lines, and splitting them across two copies is what let the
    second site miss both. `locked()` below now calls this rather than keeping
    its own copy, so there is exactly one implementation to harden (guard-2015)
    and this is not a single-use abstraction (guard-4591).

    Yields the telemetry key, which is what a caller would need to correlate
    with `write_path_status()`.
    """
    # Defaults to the manager's OWN key derivation — verified identical to
    # FileLockManager.get's `str(Path(path).resolve())`, so a caller that
    # previously did a bare `manager().get(path)` keeps acquiring the SAME
    # Lock object. Handing the resolved key in also saves the second resolve
    # syscall on the `locked()` path (see FileLockManager.get's docstring).
    key = str(Path(path).resolve()) if key is None else key
    thread_lock = _GLOBAL_MANAGER.get(path, key=key)

    with _TELEMETRY_LOCK:
        _WAITING[key] = _WAITING.get(key, 0) + 1
    try:
        # Bounded by the holder's age, not by a fixed deadline ().
        # Raises WritePathWedged ONLY when the current holder has already
        # outlived ACQUIRE_GIVEUP_SECONDS; for every younger holder this
        # blocks exactly as the bare acquire() did. On the raise this
        # contextmanager never yields, so no caller can run its body unlocked.
        acquire_or_wedge(thread_lock, key, giveup_seconds=giveup_seconds,
                         poll_seconds=poll_seconds)
    finally:
        # Runs on BOTH paths (acquired, gave up wedged, or interrupted while
        # blocked) — a waiter count that only decremented on success would
        # drift upward forever and eventually report a wedge that is not there.
        with _TELEMETRY_LOCK:
            remaining = _WAITING.get(key, 1) - 1
            if remaining > 0:
                _WAITING[key] = remaining
            else:
                _WAITING.pop(key, None)
    with _TELEMETRY_LOCK:
        _HOLDS[key] = time.monotonic()
    try:
        yield key
    finally:
        # Cleared BEFORE the lock is released: the window where the hold is
        # recorded must never outlast the window where it is actually held,
        # or an idle daemon reports a phantom hold that ages into a wedge.
        with _TELEMETRY_LOCK:
            _HOLDS.pop(key, None)
        thread_lock.release()


@contextlib.contextmanager
def locked(path: Path, *, timeout: int = 10, stale_seconds: int = 30):
    """Acquire both the threading lock AND the file lock for `path`.

    The file lock is `<path>.lock` — the SAME location _fileops uses, so
    daemon writes and fallback-path writes serialise correctly. The
    threading lock is per-process; it short-circuits the file-lock dance
    when concurrent daemon threads target the same path.

    Lock ordering (acquire): thread → file. Release order (reverse):
    file → thread. Holding the thread lock while waiting on the file lock
    is safe because no other path involves both locks in opposite order
    (the file lock has no other in-process acquirers — only out-of-process
    fallback writers).
    """
    path = Path(path)
    # CRITICAL: must match _fileops' lock-path convention EXACTLY.
    # _fileops uses path.with_suffix(".lock") — REPLACES the suffix
    # (aspirations.jsonl → aspirations.lock). If the daemon used a
    # different lock path (e.g., aspirations.jsonl.lock), daemon writes
    # and fallback-direct-python writes would race on the same file.
    # Do not "simplify" this to .suffix + ".lock".
    lock_path = path.with_suffix(".lock")
    # Resolved ONCE and shared with the lock manager: the telemetry names the
    # file a reader would recognise (not whichever alias this caller passed),
    # and the manager keys on the identical string, so instrumenting the lock
    # costs no additional syscall.
    key = str(Path(path).resolve())
    # Layer 1 — the bounded, instrumented thread-lock acquire — lives in
    # thread_locked() above. EXTRACTED, NOT COPIED: a left-behind copy here
    # would stop receiving that helper's later hardening and rot silently
    # (guard-2015), and the second caller missing exactly these lines is the
    # defect  fixed. This function adds Layer 2 (the file lock) on
    # top. Ordering is unchanged from the inline version: thread acquire →
    # record hold → file lock → yield → release file lock → clear hold →
    # thread release.
    with thread_locked(path, key=key):
        acquire_lock(lock_path, timeout=timeout, stale_seconds=stale_seconds)
        try:
            yield
        finally:
            release_lock(lock_path)


def locked_rmw(path: Path, cycle_fn, *, timeout: int = 10,
               stale_seconds: int = 30):
    """Hold the per-path lock and run a full refresh→read→modify→write cycle,
    retrying on the backend's optimistic-concurrency ConflictError (the
    own-cloud If-Match stale-lock-break race: the DDB lock that normally
    serialises cross-machine writes was force-broken by a crashed/reclaimed
    holder, so a second machine's PUT 412s against the etag this cycle read).

    `cycle_fn` is the handler's ENTIRE in-lock body — read, validate, mutate,
    write, changelog — and returns whatever the handler returns (typically a
    Response). It MUST re-read fresh on every call so each retry re-applies the
    modification on top of the peer's landed write; the daemon JSONL read
    helpers (store._read_jsonl, aspirations_write._read_jsonl) already begin
    with get_backend().refresh(path), satisfying this. A 412 means the PUT was
    rejected and NOTHING landed (see owncloud_backend._put), so re-running the
    cycle cannot double-apply.

    On LocalBackend `conflict_error` is the empty tuple, so the retry wrapper is
    a transparent single pass — byte-for-byte equivalent to a bare
    `with locked(path): return cycle_fn()` with zero added I/O. Drop-in
    replacement for that idiom on the daemon write path (#38)."""
    with locked(path, timeout=timeout, stale_seconds=stale_seconds):
        return _rmw_with_conflict_retry(path, cycle_fn)
