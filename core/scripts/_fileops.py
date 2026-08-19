"""Shared file operations: locking, history snapshots, changelog, and locked writes.

Imported by all write scripts to provide:
  - File locking (acquire_lock / release_lock)
  - Copy-on-write history (.history/ snapshots before overwriting)
  - Changelog auto-append (base_dir/changelog.jsonl)
  - Locked write functions (locked_write_jsonl, locked_append_jsonl, etc.)
  - Atomic read-modify-write primitives (locked_modify_yaml,
    locked_modify_jsonl, locked_append_jsonl_with_allocator) that hold
    the lock across the full read-modify-write cycle. Use these for any
    write where the new state depends on the existing state (counter
    increments, id allocation, dup checks, field updates).
"""

import fnmatch
import gzip
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from _paths import WORLD_DIR, META_DIR, AGENT_DIR, PROJECT_ROOT, agents_root
from _path_helpers import looks_like_cruft
# Lodestar cutover (s2): the raw lock / atomic-write primitives now live behind
# a pluggable storage backend so the same _fileops orchestration (history,
# changelog, telemetry, JSONL canary) can target a cloud object store under a
# different backend without rewriting any caller. storage_backend imports only
# stdlib, so there is no import cycle. Behavior is byte-identical when the
# backend is "local" (the default); see core/scripts/tests/test_storage_backend.py.
from storage_backend import get_backend, LocalBackend


# ---------------------------------------------------------------------------
# Structural-prevention exceptions ( — null-byte corruption defense)
# ---------------------------------------------------------------------------
# Both raised when JSONL parse-validation detects a non-empty file with zero
# parseable records — the canonical OneDrive Files-On-Demand rehydration
# corruption shape from the 2026-05-20T14:30:42 aspirations.jsonl incident.

class CorruptSourceError(Exception):
    """save_history refused to snapshot a corrupt JSONL source.

    Raised before shutil.copy2 when the live file has non-zero bytes but
    zero records parse — snapshotting that state would destroy the
    last-good-snapshot invariant read_jsonl_with_recovery depends on.
    """
    pass


class PostWriteValidationError(Exception):
    """_atomic_write_with_fallback detected post-write JSONL corruption.

    Raised AFTER restoring the target from .history (when a snapshot exists)
    so the caller's lock is still held and the file on disk is the restored
    snapshot, not the corrupt write. The corrupt write is preserved as
    <target>.post-write-corrupt for forensics.
    """
    pass


# ---------------------------------------------------------------------------
# File Locking
# ---------------------------------------------------------------------------

# Env-absent local-lock fallback state ( / rb-2764). Warn once per
# process on the fallback path so a genuinely misconfigured box stays
# observable (communication-clarity.md rule 5) without per-lock stderr spam.
_LOCK_FALLBACK_WARNED = False


def _lock_backend():
    """Return the backend for a LOCK op, with a local-file fallback for the
    bare-subprocess-on-own-cloud case (g-334-03 / rb-2764).

    On an own-cloud box ``STORAGE_BACKEND=own-cloud`` is ambient in every
    subprocess, but ``WORLD_PATH``/``META_PATH`` are exported only by *sourcing*
    ``_paths.sh``. A bare ``py -3 core/scripts/X.py`` subprocess therefore
    selects the own-cloud backend, yet ``OwnCloudBackend.from_env()`` cannot
    resolve a governed root and raises. When exactly that condition holds
    (own-cloud requested AND no governed root resolvable from the env) the ONLY
    lock that could have been requested is a LOCAL path — a governed path is
    unconstructible without the root env — so a local file lock is always
    correct, and strictly better than the prior behavior (the raise propagated
    and the caller skipped its whole read-modify-write; e.g.
    ``loop-state-bump-counters --reset-alignment`` / ``--evolution-fired``
    silently no-op'd their counter writes on every own-cloud box).

    Sourced wrappers (env present) keep the distributed DDB lock — own-cloud
    governed-path mutual exclusion is unchanged. Match the CONDITION, never the
    exception message: any ``from_env`` failure with the governed root PRESENT
    (missing bucket/table/creds) is a real misconfiguration and is re-raised.
    """
    global _LOCK_FALLBACK_WARNED
    try:
        return get_backend()
    except RuntimeError:
        kind = os.environ.get("STORAGE_BACKEND", "local").strip().lower()
        governed_root_absent = not (os.environ.get("MIND_WORLD")
                                    or os.environ.get("WORLD_PATH")
                                    or os.environ.get("MIND_META")
                                    or os.environ.get("META_PATH"))
        if kind == "own-cloud" and governed_root_absent:
            if not _LOCK_FALLBACK_WARNED:
                print("[_fileops] STORAGE_BACKEND=own-cloud but no governed root "
                      "in env (bare subprocess) — using a LOCAL file lock for "
                      "this process; sourced wrappers keep the DDB lock. "
                      "(g-334-03)", file=sys.stderr)
                _LOCK_FALLBACK_WARNED = True
            return LocalBackend()
        raise


def acquire_lock(lock_path, timeout=10, stale_seconds=30):
    """Acquire a file lock using atomic create. Breaks stale locks > stale_seconds (default 30s).

    stale_seconds tunes the staleness threshold to the caller's RMW cycle time.
    Sub-100ms cycles (working memory) should pass stale_seconds=10; subprocess-
    inside-lock paths (skill-quality scoring) may legitimately need 60s. The
    30s default preserves prior behavior for any caller that omits it.

    g-328-28: same-path callers in THIS process first wait their turn in a
    per-path FIFO (_write_queue) — under the daemon-only architecture that is
    where multi-agent contention lands (ThreadingHTTPServer threads), so the
    backend lock below is only ever contended cross-process (rare). Overload
    surfaces the DISTINCT WriteQueueFullError/WriteQueueTimeoutError, never a
    pile-up of raw lock timeouts. The turn spans acquire→release (see
    release_lock); a backend failure releases the turn before re-raising.
    """
    from _write_queue import acquire_turn, release_turn
    acquire_turn(lock_path, hold_stale=stale_seconds)
    try:
        # Raw create-if-absent + stale-break now lives in the storage backend
        # (LocalBackend.acquire_lock), moved byte-for-byte so a cloud backend can
        # map the same lock_path onto a remote lock table. The O_CREAT|O_EXCL atomic
        # create (never exists()+write — TOCTOU), the FileExistsError/PermissionError
        # POSIX-vs-Windows handling, and the stale-break all live there now.
        # Signature + semantics are unchanged for the 100+ callers of this function.
        _lock_backend().acquire_lock(lock_path, timeout=timeout,
                                     stale_seconds=stale_seconds)
    except BaseException:
        release_turn(lock_path)
        raise


def release_lock(lock_path):
    """Release a file lock, then hand the per-path FIFO turn to the next
    waiter (g-328-28 — order matters: the lock file must be gone before the
    next same-process writer is woken, or its backend acquire would spin on
    our still-present lock). The turn release is in a finally so a backend
    release failure never wedges the path's queue — the woken waiter then
    contends on whatever lock residue remains and the backend's own
    stale-break recovers it, same as cross-process contention."""
    from _write_queue import release_turn
    try:
        _lock_backend().release_lock(lock_path)
    finally:
        release_turn(lock_path)


# ---------------------------------------------------------------------------
# Cruft tripwire ()
# ---------------------------------------------------------------------------

def _cruft_tripwire(base_dir, caller, operation):
    """Return True (and emit a stack to stderr) when base_dir looks like a
    cruft mirror — drive-letter content or U+F03A in a non-root position.

    Defense-in-depth: the primary defense is `_path_helpers.absolutize` at
    the resolver. This catches new bypass call sites that didn't route
    through it. Callers MUST `return` immediately on True to skip the
    operation rather than create cruft.
    """
    if not looks_like_cruft(base_dir):
        return False
    import traceback
    print(
        f"[_fileops.{caller}] CRUFT TRIPWIRE: base_dir looks like cruft "
        f"mirror ({str(base_dir)!r}). Skipping {operation}. Caller "
        f"bypassed _absolutize — fix the call site.\nStack:",
        file=sys.stderr,
    )
    traceback.print_stack(file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# History Snapshots
# ---------------------------------------------------------------------------

# Per-base snapshot blacklist. Files whose relative path matches one of these
# entries SKIP the .history/ snapshot. changelog.jsonl entries still fire —
# the audit/provenance trail is preserved, only the full-file copy is dropped.
#
# Use this for high-churn files where snapshots have no real restore value:
#   - liveness/heartbeat files (rewritten constantly, no historical interest)
#   - append-only audit logs (the file IS the history; full-file snapshots
#     of each write multiply storage by O(N²))
#
# Keys are base-dir classifications from _classify_base() so a "presence/"
# file under WORLD doesn't accidentally exempt a same-named file under META.
# Values are tuples of relative-path patterns:
#   - trailing "/" matches everything under that directory prefix
#   - an entry containing "*" or "?" is an fnmatch glob against the whole
#     relative path (for dynamic-basename stores such as date segments)
#   - otherwise the entry is an exact relative-path match
#
# Adding entries: pair every addition with a delete of the corresponding
# .history/ subtree (free the existing snapshots) and a one-line note in
# core/config/conventions/history.md "Snapshot Blacklist" section.
_SNAPSHOT_BLACKLIST = {
    "world": (
        "presence/",            # per-agent liveness heartbeats (rewritten >>1Hz, zero historical interest)
        "board/",               # append-only coordination/findings/decisions/etc. logs — the file IS the history; full-file snapshots multiply storage O(N^2) with no restore value (changelog.jsonl keeps the audit trail). -b: pre- daemon direct-writes had accreted ~1.15G frozen board snapshots; blacklisting bounds board .history to zero in BOTH stores.
        #  item-4 prerequisite: the two utilization counter sidecars the
        # not-yet-shipped writer will flush once per maintenance tick. They are
        # FULL REWRITES (an RMW of the whole ~1-2MB sidecar), not appends, so an
        # unblacklisted flush would snapshot the whole file every tick — moving
        # the O(N^2) churn this goal exists to kill out of the content object and
        # into .history/, which is not a fix.
        #
        # Restore value is genuinely nil, which is step 1 of the history.md
        # add-procedure and the only part worth arguing: these are ADVISORY
        # retrieval-scoring counters with no cross-box read-after-write need (the
        #  design says so, and _utilization_store.load_counters repeats
        # it). A lost counter is a cosmetic scoring nuance; the changelog still
        # records every write.
        #
        # EXACT basenames, deliberately NOT a `<kind>-*.jsonl` glob. The sidecar
        # name is static (two of them), so a glob buys nothing and would match
        # BOTH `<kind>-archive.jsonl` and every date segment. Sibling precedent
        # for why that matters: coordination_merge's segment branch measured that
        # a loose glob there would OVERRIDE the archives' existing registration.
        # Names are owned by _utilization_store.counters_name(); this module is
        # imported by nearly everything and must not import it back, so the two
        # literals are pinned to counters_name() by test instead.
        "reasoning-bank-utilization.jsonl",
        "guardrails-utilization.jsonl",
        # The date SEGMENTS are deliberately absent, and that is the non-obvious
        # half. The meta/gate-firings-*.jsonl precedent above pushes the other
        # way, but it does not transfer: gate-firings segments are append-only
        # audit tail, while rb/guardrail segments are the CONTENT store and are
        # mutated in place (status -> retired, valid_to, next_review_eligible_at),
        # so they carry real restore value. Segmentation ALSO fixes their churn
        # on its own — a write touches one small day-file instead of the 20.7MB
        # whole store — so there is nothing left for a blacklist entry to buy.
    ),
    "meta": (
        "gate-firings.jsonl",   # append-only gate-decision audit log (file IS the history)
        "gate-firings-*.jsonl", # its date segments (GATE_FIRINGS_SEGMENTED flush lane, 2026-08-17): same
                                # append-only audit class, written by every box's iteration-close flush.
                                # A glob rather than the strict date shape _gate_log._SEGMENT_RE owns,
                                # because _gate_log imports THIS module (import cycle) and an over-match
                                # here only skips a snapshot of a hypothetical `gate-firings-archive.jsonl`,
                                # which would be the same audit-tail class anyway.
    ),
    "agent": (
        # Both are append-only telemetry ledgers on HOT paths, added when their
        # writers moved off bare open(...,"a") onto locked_append_jsonl
        # (-e). Same class as meta/gate-firings.jsonl above: the file IS
        # the history, and a full-file snapshot per append is O(N^2) — these fire
        # once per skill invocation and once per loop iteration respectively, so
        # unblacklisted they would snapshot a multi-thousand-line file thousands
        # of times. No .history subtree to delete alongside this addition: the
        # prior writers were bare appends, so neither store ever had one.
        "skill-invocations.jsonl",  # one append per skill fire (2 hook writers)
        "health/",                  # per-date self-health ledger, one append per iteration
    ),
}


def _classify_base(base_dir):
    """Identify whether base_dir is WORLD, META, AGENT, PROJECT/.claude, or other.

    Mirrors resolve_base_dir's ordering. Returns a short string suitable as
    a key into _SNAPSHOT_BLACKLIST.
    """
    try:
        bd = Path(base_dir).resolve()
    except (ValueError, OSError):
        return "unknown"
    if WORLD_DIR:
        try:
            if bd == WORLD_DIR.resolve():
                return "world"
        except (ValueError, OSError):
            pass
    if META_DIR:
        try:
            if bd == META_DIR.resolve():
                return "meta"
        except (ValueError, OSError):
            pass
    # Cross-agent (b34a169b port / ): classify ANY agent dir under
    # agents_root() as "agent", not only the bound AGENT_DIR — mirrors
    # resolve_base_dir so the snapshot blacklist routes to the right store for
    # every agent's files. A base is an agent dir iff it is a direct child of
    # agents_root() (agents_root/<name>); agents_root() itself is not.
    try:
        ar = agents_root().resolve()
    except (ValueError, OSError, TypeError):
        ar = None
    if ar:
        try:
            if bd != ar and bd.parent == ar:
                return "agent"
        except (ValueError, OSError):
            pass
    try:
        if bd == (PROJECT_ROOT / ".claude").resolve():
            return "claude"
    except (ValueError, OSError):
        pass
    return "unknown"


def _is_snapshot_blacklisted(base_dir, rel_path):
    """True if (base_dir, rel_path) is on the snapshot blacklist."""
    base_kind = _classify_base(base_dir)
    patterns = _SNAPSHOT_BLACKLIST.get(base_kind, ())
    if not patterns:
        return False
    rps = str(rel_path).replace("\\", "/")
    for entry in patterns:
        if entry.endswith("/"):
            if rps == entry.rstrip("/") or rps.startswith(entry):
                return True
        elif "*" in entry or "?" in entry:
            # fnmatchcase, not fnmatch: the on-disk names are exact and
            # fnmatch's platform case-folding would let a Windows box match
            # names a Linux box does not — one policy for one fleet.
            if fnmatch.fnmatchcase(rps, entry):
                return True
        elif rps == entry:
            return True
    return False


# Per-file maximum snapshot count. Files that exceed their cap have their
# oldest snapshots dropped (by timestamp parsed from filename) after each
# new write, regardless of date. Bounds growth for high-churn files where
# the date-tiered weekly prune is too coarse — "keep all of last 7 days"
# alone can be hundreds of MB for a file written hundreds of times per day.
#
# Lookup precedence in _get_snapshot_cap():
#   1. Per-base named override (e.g. {"world": {"aspirations.jsonl": 100}})
#   2. DEFAULT_SNAPSHOT_CAP
#
# A cap of None disables capping for that file (uncapped — date-tier prune
# is the only bound). Use sparingly.
#
# Capping plays nicely with gzip + blacklist:
#   - Blacklist short-circuits before cap logic (no snapshot to count).
#   - gzip just changes the byte cost per kept snapshot, not the count.
#   - The date-tiered weekly prune still runs and complements the cap.
DEFAULT_SNAPSHOT_CAP = 500

_PER_FILE_SNAPSHOT_CAP = {
    "world": {
        "aspirations.jsonl":        100,  # ~hundreds writes/day across all agents
        "reasoning-bank.jsonl":     100,
        "guardrails.jsonl":         100,
        "pipeline.jsonl":           100,
        "team-state.yaml":          100,  # cross-agent liveness signal — high churn
        "aspirations-meta.json":    50,
        # Board channels are on _SNAPSHOT_BLACKLIST ("board/") — append-only
        # audit logs get no snapshot in EITHER store, so no cap entry applies
        # (blacklist short-circuits before the cap logic — save_history's
        # _is_snapshot_blacklisted early-return).
        # Do NOT re-add board/* caps here. (-b removed them 2026-07-20.)
    },
    "meta": {
        "changelog.jsonl":          100,
        "improvement-velocity.yaml": 50,
        "goal-selection-strategy.yaml": 50,
        "spark-questions.jsonl":    100,  # 877 snapshots in 4 days on cc-04 ()
    },
}


def _get_snapshot_cap(base_dir, rel_path):
    """Return the max snapshots to keep for (base_dir, rel_path).

    Returns DEFAULT_SNAPSHOT_CAP if no per-file override; None means uncapped.
    """
    base_kind = _classify_base(base_dir)
    overrides = _PER_FILE_SNAPSHOT_CAP.get(base_kind, {})
    rps = str(rel_path).replace("\\", "/")
    if rps in overrides:
        return overrides[rps]
    return DEFAULT_SNAPSHOT_CAP


def _parse_snapshot_ts(name):
    """Return the datetime parsed from a snapshot filename, or None if unparseable.

    Mirrors history.parse_snapshot_name but lives here so save_history can
    avoid a cross-module import (history.py imports from _fileops).
    """
    if name.endswith(".gz"):
        name = name[:-3]
    if name.endswith(".meta"):
        return None
    stem = Path(name).stem
    if "_" not in stem:
        return None
    ts_str = stem.rsplit("_", 1)[0]
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


# Maximum snapshots dropped per save_history call. Bounds the in-call
# latency when the existing surplus is large (e.g. a file currently sitting
# at 5000 snapshots being capped to 100 — without this limit, the next
# save_history would unlink ~4900 files synchronously inside the locked
# write, which on OneDrive can take 30-60s). With the per-call cap, the
# excess drains over multiple writes; high-churn files converge to cap in
# <1 day at their normal write rate, low-churn files converge over the
# weekly recurring prune.
MAX_SNAPSHOTS_DROPPED_PER_CALL = 50


def _prune_to_cap(history_dir, cap):
    """Drop oldest snapshots in history_dir until count <= cap.

    Drops at most MAX_SNAPSHOTS_DROPPED_PER_CALL per invocation to bound
    in-call latency on OneDrive when the existing surplus is large.

    Unparseable filenames are left alone (could be legacy or non-snapshot
    artifacts). `.meta` sidecars are dropped with their parent snapshot.
    OneDrive lock or permission errors during unlink are swallowed — the
    next save_history call (or the weekly recurring prune) catches the rest.

    Returns the number of snapshots removed (sidecars not counted).
    """
    if cap is None:
        return 0
    try:
        entries = list(history_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return 0
    candidates = []
    for s in entries:
        if not s.is_file() or s.name.endswith(".meta") or s.name.endswith(".tmp"):
            continue
        ts = _parse_snapshot_ts(s.name)
        if ts is None:
            continue
        candidates.append((ts, s))
    if len(candidates) <= cap:
        return 0
    candidates.sort(key=lambda x: x[0])  # oldest first
    drop_count = min(len(candidates) - cap, MAX_SNAPSHOTS_DROPPED_PER_CALL)
    removed = 0
    for _ts, snap in candidates[:drop_count]:
        try:
            snap.unlink()
        except OSError as e:
            # Finding 4 (2026-05-22): record skip for observability. The skip
            # itself is intentional (OneDrive lock tolerance), but a chronic
            # failure mode (e.g., permission regression) would otherwise stay
            # invisible until storage breached. _record_fallback_hit is
            # best-effort and never raises.
            _record_fallback_hit("snapshot_prune_skip", str(snap),
                                 f"{type(e).__name__}: {e}")
            continue
        # Sidecar removal is best-effort and never fails the prune.
        meta = snap.with_suffix(snap.suffix + ".meta")
        try:
            if meta.exists():
                meta.unlink()
        except OSError as e:
            _record_fallback_hit("snapshot_prune_meta_skip", str(meta),
                                 f"{type(e).__name__}: {e}")
        removed += 1
    return removed


def save_history(path, base_dir, agent_name, summary=""):
    """Save a snapshot of the current file before it is overwritten.

    Stage 2 (2026-05-22, fix-ballooning-history): the authoritative
    snapshot store is the CAS-delta layout at
    <base_dir>/.history/{blobs,patches,snapshots}/. The legacy gzip tree
    at <base_dir>/.history/<rel-path>/<ts>...gz is kept on disk for
    read-side fallback to historical snapshots taken before the cutover,
    but is no longer written to by default.

    Rollback hatch: set FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1 to
    re-enable writes to the old gzip tree alongside the new store
    (dual-write mode). Use only if a regression in the new store is
    suspected and the operator wants legacy snapshots to keep accruing
    while the issue is investigated.

    Testing hatch: set FILEOPS_HISTORY_USE_NEW_STORE=0 to skip the new
    store write entirely. Do NOT set in production — without this writer
    AND without FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1, no snapshot lands
    at all.

    Files matching _SNAPSHOT_BLACKLIST return early — no snapshot is
    taken in EITHER store. changelog.jsonl entries (written by
    append_changelog) still fire so the audit trail is preserved.

    Args:
        path: The file being overwritten.
        base_dir: The root directory (WORLD/META/AGENT/.claude).
        agent_name: Who is making the change (for the snapshot filename).
        summary: Optional one-line description (stored in the snapshot's
                 manifest summary field).
    """
    # Both must be resolved — callers may pass unresolved paths while
    # resolve_base_dir returns resolved paths. Mismatch breaks relative_to.
    path = Path(path).resolve()
    base_dir = Path(base_dir).resolve()
    if not path.exists():
        return  # Nothing to version — new file

    if _cruft_tripwire(base_dir, "save_history", "snapshot"):
        return

    # relative_to raises ValueError when path is not under base_dir — that
    # is a programmer error in the caller. Let it surface here (original
    # behavior), BEFORE the JSONL parse-validation, so the error reflects
    # the actual misuse instead of a downstream effect.
    rel = path.relative_to(base_dir)

    # Snapshot blacklist (fix-ballooning-history-2026-05-22): skip
    # high-churn files where full-file snapshots have no restore value.
    # Runs BEFORE the JSONL parse-validate gate — blacklisted files have
    # no .history snapshots to protect, so the corrupt-source guard is
    # moot for them.
    if _is_snapshot_blacklisted(base_dir, rel):
        return

    #  outcome 1: pre-snapshot parse-validate JSONL sources.
    # Refuses to snapshot a non-empty source with zero parseable records
    # (canonical OneDrive Files-On-Demand rehydration shape, 2026-05-20
    # incident). Snapshotting corrupt state would destroy the last-good
    # snapshot read_jsonl_with_recovery depends on for recovery.
    if _is_jsonl_path(path):
        try:
            _items, _parse_errors, _total_lines = _parse_jsonl_skip_corrupt(path)
        except Exception as e:
            raise CorruptSourceError(
                f"save_history: parse-validation of {path} failed: {e!r} — "
                f"refusing to snapshot (cannot verify source is healthy)."
            )
        if _total_lines > 0 and len(_items) == 0:
            raise CorruptSourceError(
                f"save_history: source {path} has {_total_lines} lines but "
                f"0 records parsed — refusing to snapshot corrupt state. "
                f"Likely cause: OneDrive Files-On-Demand rehydration during "
                f"a concurrent write. Investigate the live file; the last "
                f"valid snapshot is preserved in .history/."
            )

    # Stage 2: legacy gzip write is opt-in (rollback hatch only). Default
    # off; new store is the authoritative writer below.
    #
    # Asymmetric error policy by design: _save_legacy_gz_snapshot RAISES
    # on failure (no try/except), while _save_to_history_store is
    # best-effort. Rationale: KEEP_LEGACY_WRITES=1 is a debug/migration
    # tool — if you've opted in and the legacy write fails, that's
    # signal you need to see loudly, not telemetry to discover later.
    # Side effect: a legacy-write failure aborts the function before the
    # new-store write fires. Acceptable; the user opted in.
    if os.environ.get("FILEOPS_HISTORY_KEEP_LEGACY_WRITES", "0") == "1":
        _save_legacy_gz_snapshot(path, base_dir, rel, agent_name, summary)

    # Stage 2 authoritative write: new CAS-delta store. Default on.
    if os.environ.get("FILEOPS_HISTORY_USE_NEW_STORE", "1") != "0":
        _save_to_history_store(path, base_dir, agent_name, summary)


def _save_legacy_gz_snapshot(path, base_dir, rel, agent_name, summary):
    """Stage 2 rollback hatch: write the legacy gzip snapshot tree.

    Default off — only fires when FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1.
    Same code as Stage 0+1's save_history body, extracted so it's
    clearly the dispensable path.

    The legacy tree at <base_dir>/.history/<rel-path>/<ts>...gz remains
    readable forever (until Stage 3 deletion). Stage 2 just stops
    appending to it; existing snapshots survive.
    """
    history_dir = base_dir / ".history" / str(rel)
    history_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    ext = path.suffix
    snapshot = history_dir / f"{timestamp}_{agent_name}{ext}.gz"
    snapshot_tmp = snapshot.with_suffix(snapshot.suffix + ".tmp")
    with open(path, "rb") as src, gzip.open(snapshot_tmp, "wb",
                                            compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, length=64 * 1024)
    os.replace(str(snapshot_tmp), str(snapshot))
    if summary:
        meta_file = snapshot.with_suffix(snapshot.suffix + ".meta")
        meta_file.write_text(summary + "\n", encoding="utf-8")
    _prune_to_cap(history_dir, _get_snapshot_cap(base_dir, rel))


def _save_to_history_store(path, base_dir, agent_name, summary):
    """Save a snapshot via the CAS-delta store (Stage 2 authoritative).

    Renamed from _shadow_write_to_history_store (Stage 1) — no longer
    "shadow" since the legacy gzip tree is no longer the authoritative
    path. This IS the snapshot write that matters.

    Best-effort: catches every exception, records the failure in
    telemetry, returns without raising. The behavioral promise: a single
    bad new-store write must not block the source file's lock-and-write
    cycle. (The cost: a failed snapshot is lost; the source file write
    still proceeds. Acceptable tradeoff because the alternative —
    raising — would block real work for transient I/O hiccups.)

    Disabled when FILEOPS_HISTORY_USE_NEW_STORE=0. DO NOT set in
    production: without this writer AND without
    FILEOPS_HISTORY_KEEP_LEGACY_WRITES=1, no snapshot lands at all.
    """
    if os.environ.get("FILEOPS_HISTORY_USE_NEW_STORE", "1") == "0":
        return

    new_encoding = None
    new_size_bytes = None
    new_chain_length = None
    error = None
    ok = False
    try:
        import _history_store
        content = path.read_bytes()
        manifest_path = _history_store.save(
            path, content, base_dir, agent=agent_name, summary=summary,
        )
        manifest = _history_store._read_manifest(manifest_path)
        new_encoding = manifest.get("encoding")
        new_size_bytes = manifest.get("size_bytes")
        new_chain_length = manifest.get("chain_length")
        ok = True
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    _record_history_save_telemetry(
        path, base_dir, agent_name,
        ok=ok, encoding=new_encoding, size_bytes=new_size_bytes,
        chain_length=new_chain_length, error=error,
    )


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------

def append_changelog(base_dir, agent_name, file_path, action, summary="", lines_changed=0):
    """Append an entry to base_dir/changelog.jsonl.

    Args:
        base_dir: Directory containing changelog.jsonl (typically WORLD_DIR).
        agent_name: Who made the change.
        file_path: The file that was changed (absolute or relative).
        action: One of: "create", "edit", "delete", "restore".
        summary: One-line description.
        lines_changed: Approximate number of lines affected.
    """
    # Plan v1 step 0.1 (2026-05-19) — None guard: WORLD_DIR/META_DIR may
    # resolve to None when no external path is configured (the fallback to
    # PROJECT_ROOT/{world,meta} was removed to refuse cruft creation). A
    # caller passing None as base_dir would TypeError on Path(None) below;
    # this guard turns that into a single-source diagnostic naming the
    # missing env var instead.
    if base_dir is None:
        raise RuntimeError(
            "append_changelog: base_dir is None — WORLD_DIR/META_DIR "
            "unresolved (no MIND_WORLD/MIND_META env, no conf entry). "
            "Bind an agent via /start, or set the env var explicitly. "
            "The PROJECT_ROOT/world|meta fallback was removed in plan v1 "
            "step 0.1 (2026-05-19) to refuse silent cruft creation."
        )
    # Resolve so the cruft check sees the same shape save_history does,
    # and so the rel_path computation below doesn't redundantly resolve.
    base_dir = Path(base_dir).resolve()

    if _cruft_tripwire(base_dir, "append_changelog", "changelog entry"):
        return

    changelog = base_dir / "changelog.jsonl"

    # : never log a changelog edit INTO the changelog itself. When
    # file_path IS base_dir/changelog.jsonl this call is (a) self-referential
    # noise a cap/rotate would immediately drop again, and (b) a hard self-
    # deadlock when reached from any locked_* function: every
    # locked_{modify,append,write}_jsonl holds path.with_suffix('.lock') across
    # its RMW and then calls append_changelog(path) post-write (7 sites:
    # L1299/1503/1645/1741/1821/1904/2025). For path==changelog.jsonl that held
    # lock IS base_dir/changelog.lock, which this function re-acquires below —
    # non-reentrant (locked_modify_jsonl docstring), so it blocks on itself
    # until acquire_lock's 10s timeout and the cap never persists (store-hygiene
    # cap on changelog.jsonl failed 4/4; meta stuck 11969>10000, world
    # 24167>20000). Skipping the self-entry is the single-source fix for all
    # call sites and the world/changelog rotate path. (rb-2736, guard-881, )
    try:
        _is_changelog_self = Path(file_path).resolve() == changelog.resolve()
    except (OSError, ValueError):
        _is_changelog_self = False
    if _is_changelog_self:
        return

    # Make file_path relative to base_dir for readability
    try:
        rel_path = str(Path(file_path).resolve().relative_to(base_dir))
    except ValueError:
        rel_path = str(file_path)

    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent_name,
        "file": rel_path,
        "action": action,
        "summary": summary,
        "lines_changed": lines_changed,
    }

    #  part (3) — session_id/body attribution is DELIBERATELY ABSENT
    # here, and must NOT be added as `os.environ.get("MIND_SID")`. Measured
    # 2026-08-03 (cc-02): the live daemon process carries the MIND_SID of
    # whichever session happened to SPAWN it (observed pid 3155606 holding
    # zeta's SID), and every agent's writes route through that one long-lived
    # process. A process-env read here would therefore stamp EVERY daemon-routed
    # changelog line with one arbitrary session's id, for as long as that daemon
    # lives — systematically wrong attribution that reads as authoritative,
    # which is strictly worse than the field being absent.
    # Correct design: thread the caller's SID down from the daemon's PER-REQUEST
    # ctx (the standard .claude/rules/path-resolution.md already sets for paths)
    # and take it as an explicit parameter. Tracked as the daemon half of
    # fix set B; do not shortcut it with a process-env read or a heuristic
    # "am I the daemon" probe.

    # Multi-agent concurrency: caller-side locks protect the file being edited
    # but NOT the shared changelog.jsonl. Two agents writing different files
    # concurrently both call append_changelog → unsynchronized appends to the
    # same file. On Windows, partial-line interleaving across OS page boundaries
    # produces corrupt JSONL that crashes downstream consumers (history.py,
    # schema-drift-sweep.py). Pattern matches locked_append_jsonl below — same
    # acquire/append/release with shared `.lock` sibling. (, fresh-eyes
    # finding from  iter-14.)
    lock_path = changelog.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        # s3 reroute DEFERRED (deliberate, not an oversight): this append is NOT
        # routed through get_backend(). changelog.jsonl is 150K+ lines and
        # appended on EVERY locked write — OwnCloudBackend.append_jsonl_record
        # is O(N) (read-all + rewrite-all + PUT, S3 has no native append), so
        # per-entry cloud routing here is a catastrophic perf cliff. In
        # own-cloud mode the changelog stays local-only until a bulk-sync /
        # rotation design lands (tracked: lodestar s3 changelog follow-up). The
        # audit-trail consumers (history.py, schema-drift-sweep) read it locally.
        with open(changelog, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Path Resolution
# ---------------------------------------------------------------------------

def resolve_base_dir(path):
    """Determine which base directory (WORLD_DIR, META_DIR, AGENT_DIR, or PROJECT_ROOT/.claude/) a path belongs to.

    Returns the base dir Path, or None if the path doesn't match any
    configured directory. Order: WORLD_DIR > META_DIR > agent dir (ANY agent
    under agents_root()) > .claude/.

    Cross-agent resolution (g-115-2169 / b34a169b port, Defect A fix): resolves
    ANY agents_root()/<name> dir, not only the MIND_AGENT-bound one, so the
    own-cloud sweep (which syncs ALL agent dirs) snapshots every agent's files
    before a pull instead of silently skipping non-bound agents. agents_root()
    itself and out-of-root paths → None.

    Phase 1 G1 patch (world/conventions/self-program-evolution.md): AGENT_DIR added
    so save_history(), append_changelog(), and the locked_write_* family
    operate on <agent>/self.md, <agent>/aspirations.jsonl, and other
    per-agent files. Previously this function returned None for agent
    paths, which meant agent-file writes skipped history snapshots and
    changelog entries.

    Phase 2 G1b additive (world/conventions/self-program-evolution.md): PROJECT_ROOT/.claude/
    added so save_history() works for .claude/skills/**/SKILL.md and .claude/rules/*.md
    edits per §14 (US-03 — SKILL/rule autonomous evolution extension). Snapshots
    land in PROJECT_ROOT/.claude/.history/.
    """
    path = Path(path).resolve()
    if WORLD_DIR:
        try:
            if path.is_relative_to(WORLD_DIR.resolve()):
                return WORLD_DIR.resolve()
        except (ValueError, OSError):
            pass
    if META_DIR:
        try:
            if path.is_relative_to(META_DIR.resolve()):
                return META_DIR.resolve()
        except (ValueError, OSError):
            pass
    # Cross-agent dir resolution (b34a169b port / , Defect A fix):
    # resolve ANY agent dir under agents_root(), not only the MIND_AGENT-bound
    # AGENT_DIR. The own-cloud sweep syncs ALL agent dirs; before this a
    # NON-bound agent path returned None here, so owncloud_sync._snapshot_before_pull's
    # fail-open guard ("if base is None: return") silently skipped the pre-pull
    # .history snapshot and the sweep clobbered every non-bound agent's files with
    # no backup. Returning the specific agent subdir (agents_root/<name>) restores
    # the snapshot for every agent. Subsumes the old AGENT_DIR-only branch —
    # AGENT_DIR == agents_root()/<bound-name>, so a bound-agent path resolves
    # identically. agents_root() itself and out-of-root paths fall through to None.
    try:
        ar = agents_root().resolve()
    except (ValueError, OSError, TypeError):
        ar = None
    if ar:
        try:
            if path != ar and path.is_relative_to(ar):
                rel = path.relative_to(ar)
                if rel.parts:
                    return (ar / rel.parts[0]).resolve()
        except (ValueError, OSError):
            pass
    claude_dir = (PROJECT_ROOT / ".claude").resolve()
    try:
        if path.is_relative_to(claude_dir):
            return claude_dir
    except (ValueError, OSError):
        pass
    return None


def _agent_name():
    """Get the current agent name, defaulting to 'system'."""
    return os.environ.get("MIND_AGENT", "system")


# ---------------------------------------------------------------------------
# UTF-8 Surrogate Gate ()
# ---------------------------------------------------------------------------
# Reject payloads containing unpaired UTF-16 surrogates (U+D800-U+DFFF). These
# code points cannot be encoded as valid UTF-8 and indicate upstream cp1252-
# decode corruption (the canonical source: stdin readers without explicit
# `sys.stdin.reconfigure(encoding="utf-8")` falling back to Windows cp1252,
# producing surrogateescape low-surrogates that round-trip into JSONL files).
# Loud failure here prevents the mojibake from landing on disk; the writer's
# caller sees a ValueError with the offending path and field.
#
# Set FILEOPS_SURROGATE_GATE=off to disable (emergency escape hatch only).

def _has_surrogates(s):
    """True if string contains any U+D800-U+DFFF code point."""
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def _validate_no_surrogates(item, path_for_error):
    """Walk item; raise ValueError if any string contains an unpaired surrogate.

    No-op when FILEOPS_SURROGATE_GATE=off. Walks dict keys+values, list
    elements, tuple elements. Non-string atoms ignored. Bounded recursion via
    Python's normal stack — deeply-nested payloads (>1000 levels) would hit
    RecursionError, but JSONL items in this codebase are flat-ish records.
    """
    if os.environ.get("FILEOPS_SURROGATE_GATE", "").lower() == "off":
        return
    stack = [item]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if _has_surrogates(cur):
                preview = cur[:80].encode("unicode_escape").decode("ascii")
                raise ValueError(
                    f"FILEOPS_SURROGATE_GATE: unpaired surrogate in payload for "
                    f"{path_for_error} — fragment={preview!r}. Likely cause: "
                    "stdin read without `sys.stdin.reconfigure(encoding=\"utf-8\")` "
                    "fell back to cp1252. Fix at ingest, then retry."
                )
        elif isinstance(cur, dict):
            stack.extend(cur.keys())
            stack.extend(cur.values())
        elif isinstance(cur, (list, tuple)):
            stack.extend(cur)


# ---------------------------------------------------------------------------
# Atomic Write with OneDrive-Lock Fallback (, hoisted)
# ---------------------------------------------------------------------------
# OneDrive Files-On-Demand reparse points block os.replace with WinError 5
# (Access denied) even when the file is pinned. The reparse-point holds a
# handle that tolerates write-through (open-truncate-write) but refuses
# rename. This helper encapsulates the retry+fallback policy so every
# locked_write_* path uses the same numbers — single source of truth for
# the lock-contention strategy. Was previously duplicated in 5 places with
# inconsistent retry counts (5 vs 8) and inconsistent fallback (none vs
# in-place rewrite); see commit c234c7e for the original aspirations.py
# implementation that the others now match.

def _atomic_write_with_fallback(target_path, write_to_handle, *,
                                fallback_counter_key=None, max_retries=10):
    """Write target_path atomically; fall back to in-place rewrite under contention.

    Strategy:
      1. Write content to <target>.tmp once (via write_to_handle callable).
      2. Attempt os.replace with exponential backoff up to max_retries
         (cap 5s/wait, ~16.7s total budget at default max_retries=10:
          ~0.1, 0.15, 0.25, 0.45, 0.85, 1.65, 3.25, 5.0, 5.0s — 9 sleeps).
         Schedule tuned to the .fallback-stats.jsonl distribution (g-285-03,
         see world/conventions/file-system-resilience.md): the prior 8-retry
         / 3s-cap schedule (~6.45s budget) fell through to fallback on
         every 30-60s sync burst (11.7% of observed fallbacks). The new
         budget covers 64%+ of observed bursts within retry. The 5-30min
         sustained-burst cases (25.7%) still fall back — fallback is
         correct, this is a latency-not-correctness tune.
      3. If all retries fail, fall back to in-place truncate-rewrite of
         target_path. Sacrifices crash-atomicity for liveness.

    Crash safety: the fallback path leaves a partial file if the process
    is killed mid-write. Mitigations rely on the caller:
      - Caller MUST hold target_path.with_suffix('.lock') so the partial
        file cannot collide with another writer.
      - Caller SHOULD have called save_history() before this helper, so a
        recent snapshot exists in <base_dir>/.history/<rel-path>/.
      - Read paths SHOULD use read_jsonl_with_recovery() (or equivalent)
        for parse-or-restore semantics on JSONL targets.

    Args:
        target_path: Path-like to the live file.
        write_to_handle: callable(open_writable_handle) writing the FULL
            file content. Called once for the tmp write, and again for the
            fallback rewrite. Must be idempotent — must produce the same
            output both times.
        fallback_counter_key: Optional string identifier; when fallback
            fires, appends a record to <WORLD_DIR>/.fallback-stats.jsonl
            keyed by this string. Recommended values: "locked_write_jsonl",
            "locked_write_json", "locked_write_yaml", "locked_modify_yaml",
            "aspirations_live".
        max_retries: Number of os.replace attempts before falling back.
            Default 8.

    Raises:
        Whatever write_to_handle raises (e.g., serialization error). On
        write_to_handle success but os.replace exhaustion, falls back
        rather than raising — that's the whole point of this helper.
    """
    target_path = Path(target_path)
    # The raw tmp-write / os.replace-retry / in-place-fallback dance now lives
    # in the storage backend (LocalBackend.atomic_write), moved verbatim — same
    # deterministic <target>.tmp name, same  retry schedule
    # (0.05*2^attempt + jitter, cap 5s, ~16.7s budget at max_retries=10), same
    # stderr diagnostics, same reparse-point in-place fallback. The backend
    # returns the contention DATA; the three orchestration concerns below stay
    # here because they reference _fileops module state (META/WORLD telemetry
    # dirs, .history restore) that the domain-free backend must not import.
    result = get_backend().atomic_write(
        target_path, write_to_handle, max_retries=max_retries)

    # : surface contention as telemetry. On the fallback path, record
    # the fallback hit FIRST (preserving the prior ordering), then the
    # contention event. A clean first-try write (retry_count==0, no fallback)
    # records nothing — only contention events are logged.
    if result.fallback_used and fallback_counter_key:
        _record_fallback_hit(fallback_counter_key, str(target_path),
                             result.error_msg)
    if result.fallback_used or result.retry_count > 0:
        # fallback_used distinguishes the success-with-retries case
        # (fallback_used=False) from the exhausted-retries fallback case.
        _record_contention_telemetry(
            target_path=str(target_path),
            retry_count=result.retry_count,
            wall_clock_ms=result.wall_clock_ms,
            fallback_used=result.fallback_used,
            error_class=result.error_class,
        )

    #  outcome 2: post-write parse canary (JSONL only). Restores from
    # .history and raises PostWriteValidationError if the just-written file is
    # non-empty but zero-parseable. Runs on BOTH the atomic and fallback paths —
    # the fallback (in-place truncate-rewrite, no crash atomicity) is riskier,
    # so the canary matters more there. The backend has already cleaned the tmp.
    _post_write_validate_jsonl(target_path)


def _record_contention_telemetry(target_path, retry_count, wall_clock_ms,
                                 fallback_used, error_class=None):
    """Append a lock-contention record to meta/file-contention-telemetry.jsonl.

    g-285-04: surfaces _atomic_write retry-loop contention as observable
    signal. One line per write that experienced retries; zero-retry writes
    produce no telemetry (zero overhead on the hot path).

    Best-effort: never raises (same pattern as _record_fallback_hit).
    Schema:
      timestamp     — local ISO 8601, agent system clock
      agent         — MIND_AGENT or "unknown"
      target        — absolute path of the contended file
      retry_count   — number of os.replace attempts that failed
      wall_clock_ms — total ms spent in retry loop (including final attempt)
      fallback_used — True if in-place rewrite was reached; False if a later
                      retry succeeded
      error_class   — last exception class name (PermissionError, OSError, ...)

    Read with: jq -c '.' <META_DIR>/file-contention-telemetry.jsonl
    Distribution: jq -s 'group_by(.fallback_used) | map({k:.[0].fallback_used,
                  n:length})' <META_DIR>/file-contention-telemetry.jsonl
    """
    if META_DIR is None:
        return
    try:
        telemetry_path = Path(META_DIR) / "file-contention-telemetry.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": _agent_name(),
            "target": target_path,
            "retry_count": retry_count,
            "wall_clock_ms": wall_clock_ms,
            "fallback_used": fallback_used,
            "error_class": error_class,
        }
        # Plain append, no lock — same best-effort policy as _record_fallback_hit.
        # Record is well under 4KB, so the write is single-line atomic on most
        # filesystems. A torn line is harmless observability noise.
        with open(telemetry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return


def _record_history_save_telemetry(path, base_dir, agent_name, *,
                                    ok, encoding, size_bytes, chain_length,
                                    error):
    """Append a history-save outcome to meta/history-save-telemetry.jsonl.

    Stage 2 (renamed from _record_shadow_telemetry — the writes are no
    longer "shadow" of an authoritative gz path; they ARE the
    authoritative path). One line per save_history call that ran the
    new-store write. Best-effort: never raises (auxiliary logging MUST
    NOT crash the writer, same pattern as _record_fallback_hit and
    _record_contention_telemetry).

    Schema:
      timestamp     — local ISO 8601, agent system clock
      agent         — calling agent_name
      file          — absolute path of the snapshotted source
      base          — absolute path of the base_dir (WORLD/META/AGENT/.claude)
      ok            — True if _history_store.save() succeeded
      encoding      — "full" | "delta" | "dropped" | None when ok=False
      size_bytes    — uncompressed source bytes (None when ok=False)
      chain_length  — 0 for full anchors, N>0 for delta-chain depth
      error         — "{ExcClass}: {message}" string when ok=False, else None

    Read with: jq -c '.' <META_DIR>/history-save-telemetry.jsonl
    Delta hit rate: jq -s 'group_by(.encoding) | map({k:.[0].encoding, n:length})' ...
    Failure scan:  jq -c 'select(.ok==false)' <META_DIR>/history-save-telemetry.jsonl
    """
    if META_DIR is None:
        return
    try:
        telemetry_path = Path(META_DIR) / "history-save-telemetry.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": agent_name,
            "file": str(path),
            "base": str(base_dir),
            "ok": ok,
            "encoding": encoding,
            "size_bytes": size_bytes,
            "chain_length": chain_length,
            "error": error,
        }
        with open(telemetry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return


def _record_fallback_hit(counter_key, target_path, error_msg):
    """Append a fallback-hit record to world/.fallback-stats.jsonl.

    Best-effort observability: never raises. Auxiliary logging MUST NOT
    crash the writer — same pattern as log_script_decision below.

    Each line: {timestamp, agent, key, target, error}

    Read with: jq -c '.' <WORLD_DIR>/.fallback-stats.jsonl
    Count last hour: jq -c 'select(.timestamp > "...")' ... | wc -l
    """
    if WORLD_DIR is None:
        return
    try:
        stats_path = Path(WORLD_DIR) / ".fallback-stats.jsonl"
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": _agent_name(),
            "key": counter_key,
            "target": target_path,
            "error": error_msg,
        }
        # Plain append, no lock — best-effort, single-line atomic on most
        # filesystems for sub-PIPE_BUF writes (this record is well under
        # 4KB). Worse case: a torn line in the stats file is harmless,
        # this is an observability sidecar not durable state.
        with open(stats_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception:
        return


# ---------------------------------------------------------------------------
# Read-Side Recovery from .history/ Snapshots ( follow-up B)
# ---------------------------------------------------------------------------
# The _atomic_write_with_fallback fallback can leave a partial file on
# crash. read_jsonl_with_recovery detects severe corruption (zero parsed
# records when lines exist, or every line failed to parse) and restores
# the most recent .history/ snapshot in place. The corrupt file is
# preserved as <path>.corrupt for forensics.

def _parse_snapshot_datetime(path):
    """Parse the embedded ISO timestamp from a snapshot filename.

    Handles all three on-disk forms:
      legacy uncompressed: <ts>_<agent>.<ext>           ts=%Y-%m-%dT%H-%M-%S
      legacy gzip:         <ts>_<agent>.<ext>.gz        ts=%Y-%m-%dT%H-%M-%S
      new-store manifest:  <ts>_<agent>.yaml            ts=%Y-%m-%dT%H-%M-%S.%f

    Returns datetime or None when the filename doesn't parse. None is
    treated by callers as "not a snapshot" (skip).

    Stage 2 (2026-05-22): cross-store merging requires comparable datetimes
    instead of lex-sortable filenames. New-store ts uses microsecond
    precision; old-store uses second precision. Lex sort across the two
    formats is WRONG because '.' (0x2E) < '_' (0x5F): a new-store
    `<ts>.000001_alpha.yaml` would lex-sort BEFORE an old-store
    `<ts>_alpha.gz` even though it's newer. Parsing fixes the order.
    """
    name = path.name
    if name.endswith(".gz"):
        name = name[:-3]
    stem = Path(name).stem
    if "_" not in stem:
        return None
    ts_str = stem.rsplit("_", 1)[0]
    for fmt in ("%Y-%m-%dT%H-%M-%S.%f", "%Y-%m-%dT%H-%M-%S"):
        try:
            return datetime.strptime(ts_str, fmt)
        except ValueError:
            continue
    return None


def _is_new_store_snapshot(path):
    """True if path is a CAS-delta manifest under .history/snapshots/.

    New-store manifests are .yaml files inside
    <base_dir>/.history/snapshots/<rel-path>/. The dual-path check
    (suffix AND parent chain) prevents false positives on any .yaml file
    elsewhere under .history/.
    """
    p = Path(path)
    if not p.name.endswith(".yaml"):
        return False
    for parent in p.parents:
        if parent.name == "snapshots" and parent.parent.name == ".history":
            return True
    return False


def _find_history_snapshots(path):
    """Return all snapshots for path, sorted newest-first by parsed datetime.

    Stage 2 (2026-05-22): merges TWO stores:
      - Legacy gzip tree at <base_dir>/.history/<rel-path>/<ts>...gz
        (Stage 0 layout — no longer written by default; readable forever
        until Stage 3 deletion).
      - CAS-delta store at <base_dir>/.history/snapshots/<rel-path>/<ts>_<agent>.yaml
        (Stage 2 authoritative — every new write lands here).

    Both stores' filenames are parsed via _parse_snapshot_datetime so the
    merged list sorts by actual datetime, not by filename lex order.
    Within the same datetime (same agent + same second/microsecond), order
    is implementation-defined but deterministic per-run.

    Skips .meta sidecars and .tmp orphans in both stores.

    Returns [] if no snapshots exist (neither directory has anything).
    """
    base_dir = resolve_base_dir(path)
    if base_dir is None:
        return []
    try:
        rel = Path(path).resolve().relative_to(Path(base_dir).resolve())
    except ValueError:
        return []
    candidates = []  # list of (datetime, Path)

    # Legacy store.
    legacy_dir = Path(base_dir) / ".history" / str(rel)
    if legacy_dir.exists():
        for p in legacy_dir.iterdir():
            if (not p.is_file()
                    or p.name.endswith(".meta")
                    or p.name.endswith(".tmp")):
                continue
            ts = _parse_snapshot_datetime(p)
            if ts is None:
                continue
            candidates.append((ts, p))

    # New CAS-delta store.
    new_dir = Path(base_dir) / ".history" / "snapshots" / str(rel)
    if new_dir.exists():
        for p in new_dir.iterdir():
            if (not p.is_file()
                    or not p.name.endswith(".yaml")
                    or p.name.endswith(".tmp")):
                continue
            ts = _parse_snapshot_datetime(p)
            if ts is None:
                continue
            candidates.append((ts, p))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in candidates]


def _find_latest_history_snapshot(path):
    """Find the most recent .history/ snapshot for path, or None.

    Thin wrapper around _find_history_snapshots — preserved for callers that
    only need the single newest snapshot. For caller paths that benefit from
    falling back to the next-newest on FileNotFoundError (Finding 2), use
    _find_history_snapshots directly.
    """
    snapshots = _find_history_snapshots(path)
    return snapshots[0] if snapshots else None


def _restore_snapshot_to_target(snapshot, target):
    """Restore .history snapshot bytes to target. Dispatches on snapshot source.

    Stage 2 (2026-05-22): snapshots can come from either the legacy gz
    tree OR the CAS-delta store. _is_new_store_snapshot classifies; the
    two paths reconstruct bytes differently:

      Legacy gzip: gzip.open + shutil.copyfileobj (streamed decompress).
      Legacy uncompressed: shutil.copy verbatim (the rare un-gzipped
        Stage 0 snapshot before the gzip compression rolled out).
      New (CAS-delta): _history_store.restore() walks the delta chain
        back to the nearest full anchor, applies patches forward, writes
        the reconstructed bytes via target.write_bytes().

    Single source of truth for snapshot→target restore. Used by:
      - read_jsonl_with_recovery (read-side JSONL corruption recovery)
      - _post_write_validate_jsonl (write-side post-validation recovery)
      - history.py cmd_restore (user-invoked restore)

    Caller is responsible for holding any required lock on target.

    No try/except wrapping — failures must propagate. The realistic
    failure modes (target locked by parallel writer, gzip integrity
    error on a truncated snapshot, missing patch in delta chain, OS
    error) are all signal the caller MUST see, not mask.
    """
    snapshot = Path(snapshot)
    target = Path(target)
    if _is_new_store_snapshot(snapshot):
        import _history_store
        base_dir = resolve_base_dir(target)
        if base_dir is None:
            raise ValueError(
                f"_restore_snapshot_to_target: target {target} is not "
                f"under any configured base_dir; cannot resolve new-store "
                f"manifest"
            )
        content = _history_store.restore(target, snapshot.name, base_dir)
        target.write_bytes(content)
        return
    if snapshot.suffix == ".gz":
        with gzip.open(snapshot, "rb") as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, length=64 * 1024)
    else:
        shutil.copy(str(snapshot), str(target))


def read_jsonl_with_recovery(path):
    """Read a JSONL file with severe-corruption recovery from .history/.

    Routine partial-line corruption (one bad line out of many): skipped
    with stderr WARN, returns the parseable subset.

    Severe corruption (zero parseable lines despite non-empty file, or
    every line failed to parse with at least 3 lines present): restore
    the most recent .history/ snapshot in place. The corrupt file is
    saved as <path>.corrupt for forensics.

    Returns: list of parsed records.
    """
    path = Path(path)
    if not path.exists():
        return []

    items, parse_errors, total_lines = _parse_jsonl_skip_corrupt(path)

    severe_corruption = (
        (total_lines > 0 and len(items) == 0) or
        (total_lines >= 3 and parse_errors == total_lines)
    )
    if not severe_corruption:
        return items

    snapshots = _find_history_snapshots(path)
    if not snapshots:
        print(f"[read_jsonl_with_recovery] ERROR: severe corruption in "
              f"{path} but no history snapshot available — returning "
              f"{len(items)} parseable records", file=sys.stderr)
        return items

    backup = Path(str(path) + ".corrupt")
    # No try/except around the backup IO — failures must propagate. The two
    # realistic failure modes (target locked by a parallel writer's fallback
    # rewrite, or genuine OS error) are both signal the caller MUST see, not
    # mask. Hiding them would silently truncate the aspirations queue.
    shutil.copy(str(path), str(backup))

    # Finding 2 retry (2026-05-22): concurrent _prune_to_cap (from another
    # agent's save_history) can drop the chosen snapshot between the
    # _find_history_snapshots scan and the restore. Walk newest-first; on
    # FileNotFoundError advance to the next. Stop on any other error
    # (truncated gzip, genuine I/O) so it propagates as before.
    snapshot = None
    for candidate in snapshots:
        try:
            # fix-ballooning-history-2026-05-22: snapshots are gzip-compressed.
            # _restore_snapshot_to_target decompresses transparently.
            _restore_snapshot_to_target(candidate, path)
            snapshot = candidate
            break
        except FileNotFoundError:
            print(f"[read_jsonl_with_recovery] WARN: snapshot {candidate.name} "
                  f"disappeared during restore (concurrent prune); trying next",
                  file=sys.stderr)
            continue
    if snapshot is None:
        print(f"[read_jsonl_with_recovery] ERROR: severe corruption in "
              f"{path} but all {len(snapshots)} snapshots disappeared "
              f"during restore — returning {len(items)} parseable records",
              file=sys.stderr)
        return items
    print(f"[read_jsonl_with_recovery] RECOVERED: restored {path.name} "
          f"from {snapshot.name}; corrupted version saved to "
          f"{backup.name}", file=sys.stderr)
    # Observability: log the recovery to fallback-stats so the same
    # dashboard surfaces both write-side fallback hits and read-side
    # recoveries — together they tell the OneDrive-contention story.
    _record_fallback_hit("read_jsonl_recovery", str(path),
                         f"severe corruption: parsed={len(items)}/"
                         f"{total_lines} lines")
    items, _, _ = _parse_jsonl_skip_corrupt(path)
    return items


def _parse_jsonl_skip_corrupt(path):
    """Parse JSONL skipping corrupt lines. Returns (items, parse_errors, total_lines)."""
    items = []
    parse_errors = 0
    total_lines = 0
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            total_lines += 1
            try:
                items.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                parse_errors += 1
                print(f"[read_jsonl] WARN: skipped corrupt line {line_no} "
                      f"in {path}: {e}", file=sys.stderr)
    return items, parse_errors, total_lines


# ---------------------------------------------------------------------------
# Structural-prevention parse canary ()
# ---------------------------------------------------------------------------
# Shared by save_history (pre-snapshot) and _atomic_write_with_fallback
# (post-write). Only fires on .jsonl paths — YAML/JSON/Markdown go through
# their own parsers and have not exhibited the null-byte corruption shape.

def _is_jsonl_path(path):
    """True iff path's suffix is .jsonl."""
    return str(path).endswith(".jsonl")


def _post_write_validate_jsonl(target_path):
    """Validate target_path after a successful write; restore from .history on corruption.

    No-op for non-JSONL paths and for legitimately-empty files. When the
    target is non-empty but zero records parse (the canonical OneDrive
    rehydration shape), preserves the corrupt write as
    <target>.post-write-corrupt, restores the most recent .history snapshot
    in place, and raises PostWriteValidationError. Caller's lock is still
    held — the restore happens atomically within the locked region.

    Fail-open: if the parse helper itself raises, logs to stderr and returns
    (no exception). The next save_history call will catch a persistently
    corrupt file via the pre-snapshot validation in save_history.
    """
    target_path = Path(target_path)
    if not _is_jsonl_path(target_path):
        return
    try:
        items, _parse_errors, total_lines = _parse_jsonl_skip_corrupt(target_path)
    except Exception as e:
        print(f"[_post_write_validate_jsonl] WARN: parse helper failed for "
              f"{target_path}: {e!r} — skipping validation", file=sys.stderr)
        return
    if total_lines == 0 or len(items) > 0:
        return
    # Severe corruption: zero parseable records in a non-empty file.
    snapshot = _find_latest_history_snapshot(target_path)
    if snapshot is None:
        raise PostWriteValidationError(
            f"_atomic_write_with_fallback: post-write parse validation failed "
            f"for {target_path} ({total_lines} lines, 0 parseable) AND no "
            f".history snapshot exists — cannot restore. Manual recovery required."
        )
    backup = Path(str(target_path) + ".post-write-corrupt")
    shutil.copy(str(target_path), str(backup))
    # fix-ballooning-history-2026-05-22: snapshots are gzip-compressed.
    # _restore_snapshot_to_target decompresses transparently.
    _restore_snapshot_to_target(snapshot, target_path)
    _record_fallback_hit(
        "post_write_validate_recovery",
        str(target_path),
        f"severe corruption: {total_lines} lines, 0 parseable; "
        f"restored from {snapshot.name}",
    )
    raise PostWriteValidationError(
        f"_atomic_write_with_fallback: post-write parse validation failed for "
        f"{target_path} ({total_lines} lines, 0 parseable). Restored from "
        f"{snapshot.name}; corrupted file saved to {backup.name}. Likely cause: "
        f"OneDrive Files-On-Demand rehydration race during write."
    )


# ---------------------------------------------------------------------------
# Locked Write Operations
# ---------------------------------------------------------------------------
# These wrap lock + history + atomic write + changelog into single calls.
# Scripts delegate their write_jsonl/write_yaml/etc. to these.
# Atomic write + OneDrive-fallback policy lives in _atomic_write_with_fallback
# above — single source of truth for retry/fallback semantics.

def locked_write_jsonl(path, items):
    """Lock → history → atomic JSONL rewrite → changelog → unlock.

    WRITE-ALL semantics (caller supplies the full content; no read-merge). Under
    own-cloud this PUT carries whatever If-Match fence the backend cached for the
    key from a prior in-process read/write, so a concurrent peer write on a
    SECOND machine can make it raise the backend's conflict_error. It is
    deliberately NOT wrapped in _rmw_with_conflict_retry — re-PUTting the same
    caller-supplied bytes would either clobber the peer (if the fence were
    refreshed) or loop-and-fail (if not); a loud 412 (nothing landed) is the safe
    outcome. For SHARED multi-writer files use locked_modify_jsonl
    (read-merge-write under the lock); write-all is for single-owner / full-
    rewrite content where last-writer-wins is intended.
    """
    # Plan v1 step 0.1 (2026-05-19) — None guard. See append_changelog.
    if path is None:
        raise RuntimeError(
            "locked_write_jsonl: path is None — likely WORLD_DIR/META_DIR "
            "unresolved (no MIND_WORLD/MIND_META env, no conf entry). "
            "The PROJECT_ROOT/world|meta fallback was removed in plan v1 "
            "step 0.1 (2026-05-19) to refuse silent cruft creation."
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock so the failure mode is
    # cheap (no lock churn on rejected writes).
    for item in items:
        _validate_no_surrogates(item, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            for item in items:
                handle.write(json.dumps(item, ensure_ascii=True) + "\n")
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_write_jsonl")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=len(items))
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Optimistic-concurrency conflict retry (G1 — machine-2 gate)
# ---------------------------------------------------------------------------
# Under the own-cloud backend a held DDB file-lock can be stale-broken mid-RMW
# (the holder's lock TTL — stale_seconds, default 30s — elapsed and a peer
# machine reacquired it), letting two machines write the same key. The second
# PUT then fails the If-Match fence and the backend raises its `conflict_error`.
# A 412 is an ATOMIC reject: nothing landed on S3, and (since OwnCloudBackend._put
# now writes the local cache only AFTER the PUT succeeds) the local cache is
# untouched too — so re-running the whole read-modify-write is safe (no
# duplicate, no partial state). The cycle MUST re-refresh + re-read so it sees
# the peer's write and re-applies the mutation on top of it. Bounded at 3
# attempts: a stale-break is rare and self-clearing. On LocalBackend
# `conflict_error` is the empty tuple, so the except clause matches nothing and
# this is a transparent single pass (zero added I/O on the default path).
_CONFLICT_RETRY_CAP = 3


def _conflict_backoff(attempt):
    """Short jittered backoff (seconds) between conflict re-tries."""
    return min(0.05 * (2 ** attempt) + random.uniform(0, 0.05), 1.0)


# Aggregate wall-clock budget for ONE conflict-retry composition ().
# _CONFLICT_RETRY_CAP bounds the COUNT of cycles; nothing bounded their PRODUCT.
# Each cycle issues several S3/DDB operations, and botocore independently retries
# each one up to max_attempts, so the real exposure was
# cap x ops-per-cycle x attempts x timeout -- a multi-minute caller hang built
# entirely out of individually-bounded layers. THIS IS THE POINT: the defect was
# never a missing timeout (every layer had one), it was a missing AGGREGATE
# DEADLINE over their composition.
_RMW_DEADLINE_DEFAULT = 120.0


def _rmw_deadline_seconds():
    """Wall-clock budget for one _rmw_with_conflict_retry composition.

    Resolved per-call rather than cached at import so the env is always honored
    (same reasoning as wm_path) and so tests can set a tiny budget without
    reloading the module. A non-numeric or non-positive value falls back to the
    default: failing back to BOUNDED is the safe direction, since the whole
    purpose here is that no configuration accidentally restores the unbounded
    composition this constant exists to cap.
    """
    raw = os.environ.get("MIND_RMW_DEADLINE_SECONDS", "").strip()
    if not raw:
        return _RMW_DEADLINE_DEFAULT
    try:
        val = float(raw)
    except ValueError:
        return _RMW_DEADLINE_DEFAULT
    return val if val > 0 else _RMW_DEADLINE_DEFAULT


def _rmw_with_conflict_retry(path, cycle_fn):
    """Run ``cycle_fn()`` (a full refresh->read->modify->write cycle, held under
    the caller's lock) and retry it on the active backend's optimistic-concurrency
    ``conflict_error``, up to ``_CONFLICT_RETRY_CAP`` attempts OR until the
    aggregate wall-clock deadline is spent, whichever comes first. Returns
    cycle_fn's value. ``cycle_fn`` MUST re-read fresh each call (begin with
    ``get_backend().refresh(path)``) so each retry re-applies the modifier on the
    latest remote state. Re-raises the conflict on the final attempt.

    HONEST BOUND -- state it this way, do not round it down to `deadline`. The
    budget is checked BETWEEN cycles and cannot interrupt a cycle already in
    flight, so the true ceiling is::

        deadline + (duration of one in-flight cycle)

    That still converts an unbounded compounding product into "at most one more
    cycle after the budget is spent", which is what lets the callers' existing
    fail-open paths fire instead of blocking for minutes.

    Deadline exhaustion re-raises the SAME conflict exception that cap
    exhaustion already raises. That is deliberate, not laziness: every caller
    already handles that path and releases its lock through the same ``finally``,
    so this introduces no new failure mode and cannot strand a lock or leave a
    half-applied write (guard-2227 -- the read is still asserted before any
    write fires, because an aborted cycle simply never reaches its write).
    """
    conflict_cls = get_backend().conflict_error
    deadline_at = time.monotonic() + _rmw_deadline_seconds()
    for c_attempt in range(_CONFLICT_RETRY_CAP):
        try:
            return cycle_fn()
        except conflict_cls:
            if c_attempt == _CONFLICT_RETRY_CAP - 1:
                raise
            # monotonic, not wall time: a clock adjustment mid-RMW must not
            # extend or collapse the budget.
            if time.monotonic() >= deadline_at:
                raise
            time.sleep(_conflict_backoff(c_attempt))


# ---------------------------------------------------------------------------
# Local-WM stale-lock-steal CAS retry ( mechanism B — )
# ---------------------------------------------------------------------------
# The three loop_state writers — loop-state-bump-counters.py,
# recurring-loop-state-mutate.py, and the daemon wm_write.set_slot — each hold
# working-memory.yaml's .lock (stale_seconds=10) across a fresh-read
# read-modify-write. The lock normally serialises them, but acquire_lock
# STALE-BREAKS a lock whose file mtime exceeds stale_seconds: a holder that
# stalls >10s mid-RMW (OneDrive/daemon write latency, guard-597) has its lock
# stolen, two critical sections overlap, and the later write clobbers the
# earlier counter increment (observed bravo session 85; analysis  and
# the loop-state-integrity tree node "Lock-Atomicity & The Stale-Steal Residual
# Race").
#
# This is the ROBUST tier of 's fix hierarchy: optimistic concurrency
# keyed on slot_meta[slot].update_count (already maintained by every writer's
# _update_modified, guard-540/guard-449). A writer captures update_count BEFORE
# mutating; just before writing it re-reads the on-disk update_count; if it
# changed, a peer wrote during the stall, so it re-reads fresh and re-applies
# the mutation. Same retry SHAPE as _rmw_with_conflict_retry above (reuses
# _conflict_backoff), but the conflict token is the local update_count, not an
# S3 etag: WM is local-only and is DELIBERATELY excluded from the backend
# conflict path (see mind_api/src/endpoints/wm_write._write_wm docstring).
#
# Pure CONTROL helper: it performs NO filesystem write (no os.replace / tmp
# rename), so it is NOT a locked_write_* function and guard-472's
# _atomic_write_with_fallback requirement does not apply — each caller delegates
# its existing byte-compat read/write via callbacks (CLI yaml.safe_dump vs the
# daemon's default-Dumper _write_wm are preserved unchanged). The caller MUST
# already hold the slot file's .lock; this helper only re-reads to detect a
# steal that broke that lock mid-cycle. rb-1536 (DDB CAS-via-conditional-guard)
# is the cross-store analogue of this optimistic-concurrency pattern.

_LOOP_STATE_CAS_RETRY_CAP = 3


def _slot_update_count(wm, slot):
    """Extract slot_meta[slot].update_count from a working-memory dict.

    Returns 0 when the dict, its slot_meta, the slot's meta, or the counter is
    absent or non-integer — a missing counter compares equal to a fresh writer's
    captured 0, so the no-contention first-write path is unaffected."""
    if not isinstance(wm, dict):
        return 0
    meta = wm.get("slot_meta")
    if not isinstance(meta, dict):
        return 0
    sm = meta.get(slot)
    if not isinstance(sm, dict):
        return 0
    try:
        return int(sm.get("update_count", 0))
    except (TypeError, ValueError):
        return 0


def durable_write_text(tmp_path, text, encoding="utf-8"):
    """Write text to tmp_path and fsync BEFORE the caller's atomic rename.

    A bare write_text() + os.replace() survives a crash in metadata only:
    NTFS journals the rename while the data blocks are still unflushed, so
    the renamed file can come back as all-0x00 content (canonical incident:
    charlie session d600a945 working-memory.yaml, 8140 null bytes after a
    60+ min autocompact hang — g-001-44). flush+fsync pins the data to disk
    before the rename can make it the live file.
    """
    with open(tmp_path, "w", encoding=encoding) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def loop_state_cas_retry(read_fn, mutate_fn, write_fn, *,
                         slot="loop_state",
                         retry_cap=_LOOP_STATE_CAS_RETRY_CAP,
                         backoff_fn=None):
    """Stale-lock-steal-safe read-modify-write for a structured WM slot.

    The CALLER must already hold the slot file's .lock. Within that lock::

        for attempt in range(retry_cap):
            wm    = read_fn()                       # fresh read each attempt
            token = slot_meta[slot].update_count    # captured BEFORE mutate
            if not mutate_fn(wm):                    # apply delta + _update_modified
                return {"noop": True, ...}           # idempotent no-op -> no write
            if _slot_update_count(read_fn()) == token:   # re-read: peer didn't write
                write_fn(wm); return {"conflicted": attempt>0, ...}
            sleep(backoff(attempt))                  # peer stale-broke lock & wrote -> retry
        write_fn(wm); return {"exhausted": True, ...}   # retries spent -> fail-open commit

    Contract:
      - ``read_fn()`` returns a fresh wm dict parsed from disk (may raise; the
        caller's existing fail-open try/except wraps this call).
      - ``mutate_fn(wm)`` mutates ``wm[slots][slot]`` IN PLACE reading every value
        from the passed-in (fresh) wm — never a snapshot captured before the call —
        so a retry re-applies the delta on the peer's landed write. It MUST call
        the caller's ``_update_modified(wm, slot)`` (advancing update_count). It
        returns truthy to commit, or falsy for an idempotent no-op (e.g. a goal
        already counted) in which case NO write happens.
      - ``write_fn(wm)`` performs the caller's existing atomic write of ``wm``.

    A stale-lock-steal (a peer broke this holder's lock after a >10s stall and
    wrote) bumps the on-disk update_count past ``token``, so the verify re-read
    mismatches and the cycle re-reads + re-applies — no lost increment. A
    verify-read FAILURE is treated as a conflict (retry), never as a synthesised
    no-conflict (guard-160). On the common no-contention path the verify matches
    on attempt 0: one extra read, identical write behaviour. When all retries
    conflict the helper commits the last attempt (fail-open: a silently-dropped
    increment is worse than a re-applied one, which the writers' own idempotency
    guards already dedupe) and sets ``exhausted=True``. Returns a dict with keys
    ``noop``, ``attempts``, ``conflicted``, ``exhausted``."""
    if backoff_fn is None:
        backoff_fn = _conflict_backoff
    wm = None
    for attempt in range(retry_cap):
        wm = read_fn()
        token = _slot_update_count(wm, slot)
        if not mutate_fn(wm):
            return {"noop": True, "attempts": attempt + 1,
                    "conflicted": False, "exhausted": False}
        try:
            conflict = _slot_update_count(read_fn(), slot) != token
        except Exception:
            conflict = True
        if not conflict:
            write_fn(wm)
            return {"noop": False, "attempts": attempt + 1,
                    "conflicted": attempt > 0, "exhausted": False}
        time.sleep(backoff_fn(attempt))
    # Retries exhausted — fail-open commit of the last mutated state.
    write_fn(wm)
    return {"noop": False, "attempts": retry_cap,
            "conflicted": True, "exhausted": True}


def locked_append_jsonl(path, item):
    """Lock → history → JSONL append → changelog → unlock.

    The backend append is wrapped in ``_rmw_with_conflict_retry``: a 412 from the
    own-cloud If-Match fence (a stale-lock-break race on a second machine) means
    the PUT was atomically rejected — nothing landed — so re-running the
    backend's force-fresh-read+append+PUT cannot duplicate the record. This is
    distinct from retrying an OS-level append after a partial write (which WOULD
    risk a duplicate and is still not done). On LocalBackend the retry is a
    transparent single pass (``conflict_error == ()``).
    """
    # Plan v1 step 0.1 (2026-05-19) — None guard. See append_changelog.
    if path is None:
        raise RuntimeError(
            "locked_append_jsonl: path is None — likely WORLD_DIR/META_DIR "
            "unresolved (no MIND_WORLD/MIND_META env, no conf entry). "
            "The PROJECT_ROOT/world|meta fallback was removed in plan v1 "
            "step 0.1 (2026-05-19) to refuse silent cruft creation."
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock.
    _validate_no_surrogates(item, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)
        # s3 reroute: route the append through the active backend. LocalBackend
        # does the identical raw open(path,"a")+json.dumps(ensure_ascii) append
        # (byte-identical); OwnCloudBackend does a force-fresh read + append +
        # PUT so the record reaches S3. Caller holds the lock; the backend
        # method does not re-lock. G1: retry the backend append on an If-Match
        # conflict (safe — a 412 means nothing landed; see docstring).
        _rmw_with_conflict_retry(
            path, lambda: get_backend().append_jsonl_record(path, item))
        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=1)
    finally:
        release_lock(lock_path)


# ---------------------------------------------------------------------------
# Atomic Read-Modify-Write for JSONL (g-280-* concurrency hardening)
# ---------------------------------------------------------------------------
# Without these, every script that did
#   items = read_jsonl(path)
#   items[i] = mutated; or items.append(new); or check_no_duplicate_id(items, id)
#   locked_append_jsonl(path, item)  # or locked_write_jsonl(path, items)
# had a race window between the unlocked read and the locked write. Two
# concurrent agents could both observe the same baseline and the second
# writer would silently clobber the first. The team-state.py audit traced
# this back to the read-then-locked-write split (see git log e2276b5 for
# the team-state fix that motivated extending the same primitive to JSONL).
#
# Both primitives below hold the lock across the full read-modify-write.
# Choose by call shape:
#   - locked_modify_jsonl: rewrite-all (update-path: increment counter,
#     mutate field, replace record). O(N) write.
#   - locked_append_jsonl_with_allocator: append-only with allocator-
#     computed id (add-path: rb_add, guard_add, sig_add, sq_add, journal,
#     board.cmd_post). O(1) write.
#
# NEITHER lock is re-entrant. Calling either primitive on the SAME path
# from inside modifier_fn / build_record_fn deadlocks until the 30s
# stale-lock break. Modifier/build callbacks must be pure in-memory work.

def locked_modify_jsonl(path, modifier_fn, *, initial=None):
    """Atomic read-modify-write of a JSONL file.

    Holds the lock across the entire cycle: read items list → invoke
    modifier_fn(items) → write the returned list atomically. Two concurrent
    callers cannot read the same baseline and clobber each other.

    Args:
      path: Path to the JSONL file.
      modifier_fn: Callable[[list[dict]], list[dict]] receiving the current
        records list and returning the new list to persist. May mutate the
        list in place and return it, or build a new list. Returning None is
        equivalent to returning the (possibly mutated) input list.
      initial: List used as starting state when the file does not exist. If
        None and the file is missing, modifier_fn receives [].

    Returns:
      The list of records that was written (modifier_fn's return value).

    Race semantics: same lineage as locked_modify_yaml (e2276b5). The read
    happens INSIDE the lock that guards the write — any concurrent writer
    either (a) finishes fully before our read, or (b) blocks until our
    write completes. No clobber.

    Recovery: severe-corruption recovery (zero parseable lines despite
    non-empty file, or every line failed to parse) restores the most
    recent .history/ snapshot in place via read_jsonl_with_recovery
    semantics. The corrupt file is preserved as <path>.corrupt for
    forensics. Routine partial-line corruption is skipped with a stderr
    WARN and the parseable subset is returned.

    Lock is NOT re-entrant. modifier_fn must NOT call locked_modify_jsonl,
    locked_append_jsonl, locked_append_jsonl_with_allocator, or
    locked_write_jsonl on the same path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()

        # G1: the full refresh->read->modify->write cycle, re-runnable on an
        # own-cloud If-Match conflict (a stale-lock-break race on a second
        # machine). _rmw_with_conflict_retry re-invokes _cycle so each retry
        # re-refreshes (new fence token), re-reads the peer's write, and
        # re-applies modifier_fn on top. Single transparent pass on LocalBackend.
        # save_history runs per-attempt by design — it snapshots each remote
        # version about to be overwritten (the rare extra snapshot on a retry is
        # harmless and gives a richer recovery trail).
        def _cycle():
            # s3 reroute: force-fresh the local cache from the backend before the
            # in-lock read, so the read-modify-write starts from the latest remote
            # state (own-cloud lost-update prevention, fix #2) and materializes a
            # remote-only file locally. No-op on LocalBackend (reads nothing). Done
            # unconditionally — even if the file is absent locally, the cloud may
            # hold it.
            get_backend().refresh(path)
            # Read inside the lock with the same severe-corruption recovery
            # semantics read_jsonl_with_recovery provides. Routine partial-line
            # corruption returns the parseable subset; severe corruption
            # restores from .history/ with the corrupt file preserved as
            # <path>.corrupt. Transient Windows PermissionError (anti-virus,
            # OneDrive sync) gets retry — symmetric to locked_modify_yaml.
            # The retry loop exits ONLY via break (items set) or raise (last
            # attempt) — there is no fall-through path, so no defensive
            # post-loop None-check is needed.
            if path.exists():
                read_retries = 5
                for attempt in range(read_retries):
                    try:
                        items = read_jsonl_with_recovery(path)
                        break
                    except (PermissionError, OSError) as e:
                        if attempt == read_retries - 1:
                            raise
                        wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                        print(f"locked_modify_jsonl read-retry "
                              f"{attempt+1}/{read_retries}: {e} "
                              f"(waiting {wait:.2f}s)", file=sys.stderr)
                        time.sleep(wait)
            else:
                items = list(initial) if initial is not None else []

            # Modify
            new_items = modifier_fn(items)
            if new_items is None:
                new_items = items

            #  lineage: validate post-modify, pre-write. Cannot validate
            # before acquire_lock because new_items doesn't exist until
            # modifier_fn runs inside the lock. Surrogate-laced output fails
            # loud before yaml/json serialization corrupts the on-disk file.
            for item in new_items:
                _validate_no_surrogates(item, path)

            # Write inside the same lock
            if base_dir:
                save_history(path, base_dir, agent)

            def _write(handle):
                for item in new_items:
                    handle.write(json.dumps(item, ensure_ascii=True) + "\n")
            _atomic_write_with_fallback(
                path, _write, fallback_counter_key="locked_modify_jsonl")
            return new_items

        new_items = _rmw_with_conflict_retry(path, _cycle)

        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=len(new_items))
        return new_items
    finally:
        release_lock(lock_path)


def locked_append_jsonl_with_allocator(path, build_record_fn, *, initial=None):
    """Atomic append with allocator-computed fields. O(1) write — no rewrite.

    Holds the lock across read → build_record_fn(items) → append. The
    builder sees the current records inside the lock, so allocator output
    (next id, next session number, next message-NNN counter) cannot collide
    with a concurrent caller's allocator output on the same file.

    Args:
      path: Path to the JSONL file.
      build_record_fn: Callable[[list[dict]], dict] receiving the current
        records list and returning the new record to append. The record
        MUST be complete (id, timestamps, etc.) — the primitive does not
        mutate it.
      initial: List used as starting state when the file does not exist
        (rarely matters for append paths; included for symmetry).

    Returns:
      The record that was appended (build_record_fn's return value).

    Use this for add-paths (rb_add, guard_add, board.cmd_post, journal,
    sig_add, sq_add). For update-paths (mutating an existing record's
    field, incrementing a counter), use locked_modify_jsonl instead.

    Lock is NOT re-entrant. build_record_fn must NOT call any locked_*
    primitive on the same path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()

        # G1: refresh->read->build->append cycle, re-runnable on an own-cloud
        # If-Match conflict (stale-lock-break race). Re-reading fresh on each
        # retry also preserves the allocator invariant — the next id is computed
        # from the latest remote items, so a peer's concurrent allocation cannot
        # produce a duplicate id. Single transparent pass on LocalBackend.
        def _cycle():
            # s3 reroute: force-fresh the local cache before the in-lock read so the
            # allocator sees the latest remote items (else two machines could
            # allocate the SAME next-id from stale local state -> duplicate). No-op
            # on LocalBackend; materializes a remote-only file from S3.
            get_backend().refresh(path)
            # Read inside the lock for allocator visibility. Same recovery
            # path as locked_modify_jsonl — severe corruption restores from
            # .history/. Retry loop exits via break (items set) or raise (last
            # attempt) — no fall-through, no defensive None-check needed.
            if path.exists():
                read_retries = 5
                for attempt in range(read_retries):
                    try:
                        items = read_jsonl_with_recovery(path)
                        break
                    except (PermissionError, OSError) as e:
                        if attempt == read_retries - 1:
                            raise
                        wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                        print(f"locked_append_jsonl_with_allocator read-retry "
                              f"{attempt+1}/{read_retries}: {e} "
                              f"(waiting {wait:.2f}s)", file=sys.stderr)
                        time.sleep(wait)
            else:
                items = list(initial) if initial is not None else []

            # Build record with allocator visibility into existing items
            new_record = build_record_fn(items)
            if new_record is None:
                raise ValueError(
                    "build_record_fn returned None — must return the record "
                    "to append (or raise to abort)")

            # Validate post-build, pre-append
            _validate_no_surrogates(new_record, path)

            # Append via the backend. LocalBackend keeps the O(1) raw append (the
            # perf advantage over locked_modify_jsonl); OwnCloudBackend does a
            # force-fresh read + append + PUT (O(N) — S3 has no native append) so
            # the record reaches the cloud. save_history snapshots the file BEFORE
            # the append so a single-record-back undo is always possible.
            if base_dir:
                save_history(path, base_dir, agent)
            get_backend().append_jsonl_record(path, new_record)
            return new_record

        new_record = _rmw_with_conflict_retry(path, _cycle)
        if base_dir:
            append_changelog(base_dir, agent, path, "edit",
                             lines_changed=1)
        return new_record
    finally:
        release_lock(lock_path)


def next_id_for_prefix(items, prefix, *, id_field="id", pad_width=0,
                        separator="-"):
    """Compute the next sequential id for a prefix-NNN id scheme.

    Scans items for ids matching ^{prefix}{separator}(\\d+)$ (any digit
    count), returns f"{prefix}{separator}{N+1:0{pad_width}d}". If no items
    match, returns f"{prefix}{separator}{1:0{pad_width}d}".

    Non-conforming ids (legacy formats, different prefixes, ids with
    suffixes) are silently skipped — only valid prefix-NNN ids count
    toward max. This means an existing rb-9999z record will not block
    rb-{N+1} where N is the highest valid rb-NNN.

    Args:
      items: List of records (dicts).
      prefix: ID prefix without separator (e.g., "rb", "guard", "sig", "de"
        for separator="-"; "sq-c" for the spark-question candidate format
        sq-c01 with separator="").
      id_field: Field name where the id lives. Default "id".
      pad_width: Zero-pad the numeric portion to this many digits. Default
        0 (no padding — produces "rb-735" not "rb-00735"). Use 3 for
        spark-question questions ("sq-001" format), 2 for candidates
        ("sq-c01" format).
      separator: Character between prefix and number. Default "-". Pass
        "" for ids like "sq-c01" where the prefix already ends with the
        format ("sq-c") and the digits attach directly.

    Returns:
      The next id string.
    """
    import re
    pattern = re.compile(
        rf"^{re.escape(prefix)}{re.escape(separator)}(\d+)$")
    max_n = 0
    for item in items:
        match = pattern.match(item.get(id_field, "") or "")
        if match:
            n = int(match.group(1))
            if n > max_n:
                max_n = n
    next_n = max_n + 1
    if pad_width > 0:
        return f"{prefix}{separator}{next_n:0{pad_width}d}"
    return f"{prefix}{separator}{next_n}"


def locked_write_json(path, data):
    """Lock → history → atomic JSON rewrite → changelog → unlock.

    WRITE-ALL — same multi-machine caveat as locked_write_jsonl: under own-cloud
    it can raise the backend's conflict_error on a stale cached If-Match fence
    and is intentionally not conflict-retried. Use a read-merge-write primitive
    for shared multi-writer files.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock.
    _validate_no_surrogates(data, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            json.dump(data, handle, indent=2, ensure_ascii=True)
            handle.write("\n")
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_write_json")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


def log_script_decision(script_name, record):
    """Append a per-decision audit record to world/<script-name>-log.jsonl.

    Lightweight append-only: lock + append + unlock. Skips save_history and
    changelog — these logs are append-forever audit trails, not structural
    data worth versioning.

    DESIGN — do not "fix" without reading both points:
      1. Entry injection order: `{**record, "timestamp": ..., "agent": ...}`
         is intentional. Helper-owned metadata MUST win if a caller passes
         a field of the same name. Do NOT reorder.
      2. Broad `except Exception: return` is intentional. Audit logging is
         auxiliary; infrastructure failure (disk full, permission denied,
         lock contention) MUST NOT crash the caller's main decision flow.
         Do NOT narrow this except — `locked_append_jsonl` fails loudly
         on purpose because it writes durable state; this helper does not.
    """
    if WORLD_DIR is None:
        return
    try:
        path = Path(WORLD_DIR) / f"{script_name}-log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            **record,
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "agent": _agent_name(),
        }
        lock_path = path.with_suffix(".lock")
        # Audit-log fast path: tiny single-line append, sub-millisecond hold.
        # 5s stale-break is 5000x cycle time — short enough to recover from a
        # crashed mid-write quickly without false-breaking a healthy holder.
        acquire_lock(lock_path, stale_seconds=5)
        try:
            # s3 reroute DEFERRED (deliberate): NOT routed through the backend.
            # This append uses json.dumps(..., default=str) to tolerate datetime
            # / Path values; the backend's append_jsonl_record has no default=str
            # and would TypeError. These are fire-and-forget local audit logs
            # (note the broad except below), not cross-machine domain state, so
            # local-only is acceptable. Routing would require pre-serialization
            # (lodestar s3 follow-up, low priority).
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
        finally:
            release_lock(lock_path)
    except Exception:
        return


def locked_write_yaml(path, data):
    """Lock → history → atomic YAML rewrite → changelog → unlock.

    WRITE-ALL — same multi-machine caveat as locked_write_jsonl: under own-cloud
    it can raise the backend's conflict_error on a stale cached If-Match fence
    and is intentionally not conflict-retried. Use locked_modify_yaml for shared
    multi-writer files.
    """
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    # : validate BEFORE acquiring the lock so the failure mode is
    # cheap (no lock churn on rejected writes). Mirrors locked_write_json.
    _validate_no_surrogates(data, path)
    acquire_lock(lock_path)
    try:
        agent = _agent_name()
        if base_dir:
            save_history(path, base_dir, agent)

        def _write(handle):
            yaml.dump(data, handle, Dumper=yaml.CSafeDumper,
                      default_flow_style=False, allow_unicode=True,
                      sort_keys=False)
        _atomic_write_with_fallback(
            path, _write, fallback_counter_key="locked_write_yaml")

        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
    finally:
        release_lock(lock_path)


def locked_modify_yaml(path, modifier_fn, initial=None):
    """Atomic read-modify-write on a YAML file. Holds the lock across the
    ENTIRE cycle, closing the race where two agents read the same baseline
    and the second writer clobbers the first.

    Args:
      path: Path to the YAML file.
      modifier_fn: Callable accepting the current data (dict) and returning
        the new data (dict) to write. May mutate in place and return the
        same object, or construct a new dict.
      initial: Dict to use as the starting state when the file does not
        exist. If None and the file is missing, the modifier receives {}.

    Returns:
      The data written to the file (modifier_fn's return value).

    Race semantics (g-240-52): cmd_update-style sites previously did
      state = read_state(); state[x] = y; write_state(state)
    The read was unlocked, so two concurrent writers could both observe
    the baseline before either wrote, and the second write clobbered the
    first. locked_modify_yaml reads INSIDE the lock that guards the write,
    so any other writer either (a) writes fully before our read starts,
    or (b) blocks until our write completes. Either way, no silent
    clobber.
    """
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base_dir = resolve_base_dir(path)
    lock_path = path.with_suffix(".lock")
    acquire_lock(lock_path)
    try:
        agent = _agent_name()

        # G1: refresh->read->modify->write cycle, re-runnable on an own-cloud
        # If-Match conflict (a stale-lock-break race on a second machine).
        # _rmw_with_conflict_retry re-invokes _cycle so each retry re-refreshes
        # (new fence token), re-reads the peer's write, and re-applies
        # modifier_fn on top. Single transparent pass on LocalBackend.
        def _cycle():
            # s3 reroute: force-fresh the local cache from the backend before the
            # in-lock read so the read-modify-write sees the latest remote state
            # (own-cloud lost-update prevention) and materializes a remote-only
            # file. No-op on LocalBackend; the CSafeLoader read below then operates
            # on the now-current local file.
            get_backend().refresh(path)
            # Read inside the lock — this is the critical move. Retry on
            # transient Windows PermissionError from concurrent file-handle
            # churn (anti-virus, indexing, OneDrive sync, etc.); symmetric to
            # the write-retry loop below. Without this, a racer's read can
            # fail even when the lock is held by no other agent.
            data = None
            if path.exists():
                read_retries = 5
                for attempt in range(read_retries):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            # CSafeLoader (libyaml-backed) is ~6× faster than
                            # yaml.safe_load on large YAMLs (verified: _tree.yaml
                            # 758KB load 7.3s→1.25s). Required for the tree-sync
                            # PostToolUse hook to complete within its 5s timeout.
                            # CSafeDumper produces byte-identical output. DO NOT
                            # downgrade to safe_load — single source of truth.
                            data = yaml.load(f, Loader=yaml.CSafeLoader)
                        break
                    except (PermissionError, OSError) as e:
                        if attempt == read_retries - 1:
                            raise
                        wait = 0.05 * (2 ** attempt) + random.uniform(0, 0.1)
                        print(f"locked_modify_yaml read-retry {attempt+1}/{read_retries}: "
                              f"{e} (waiting {wait:.2f}s)", file=sys.stderr)
                        time.sleep(wait)
                # NOT dead code — diverges from locked_modify_jsonl pattern.
                # PyYAML parse returns None for empty/whitespace/comment-only
                # YAML files (regardless of loader). read_jsonl_with_recovery
                # always returns a list, so its sibling post-loop None-check
                # was dead and got removed.
                # Here it is REQUIRED — without it, an empty YAML file produces
                # data=None which crashes modifier_fn / _validate_no_surrogates.
                # Verified by  (2026-05-08).
                if data is None:
                    data = dict(initial) if initial is not None else {}
            else:
                data = dict(initial) if initial is not None else {}

            # Modify
            new_data = modifier_fn(data)
            if new_data is None:
                # Allow the modifier to signal "no write" by returning None; fall
                # back to the mutated-in-place data. If the caller truly wants to
                # skip the write they can short-circuit before calling this.
                new_data = data

            # : validate post-modify, pre-write. Cannot validate before
            # acquire_lock here (unlike locked_write_yaml) because new_data does
            # not exist until modifier_fn runs inside the lock. The walk is cheap
            # and short-circuits on the kill-switch; it raises before yaml.dump
            # so a surrogate-laced modifier_fn fails loud without writing the
            # tmp file or saving a .history/ snapshot of the corrupted attempt.
            _validate_no_surrogates(new_data, path)

            # Write inside the same lock
            if base_dir:
                save_history(path, base_dir, agent)

            def _write(handle):
                yaml.dump(new_data, handle, Dumper=yaml.CSafeDumper,
                          default_flow_style=False, allow_unicode=True,
                          sort_keys=False)
            _atomic_write_with_fallback(
                path, _write, fallback_counter_key="locked_modify_yaml")
            return new_data

        new_data = _rmw_with_conflict_retry(path, _cycle)

        if base_dir:
            append_changelog(base_dir, agent, path, "edit")
        return new_data
    finally:
        release_lock(lock_path)
