#!/usr/bin/env python3
"""Shared JSONL rotation/compaction helper -- the KEYSTONE of the 
data-store-hygiene cohort (g-333-12).

THE PROBLEM (alpha audit, msg-20260624-103842-alpha-2463)
  One disease, three shapes: append-only JSONL + logical-only retirement + no
  file-on-disk rotation. Stores like journal.jsonl, meta/evolution-log.jsonl,
  world/changelog.jsonl, world/board/*.jsonl, meta/gate-firings.jsonl,
  world/presence/<agent>.jsonl grow without bound. Every write is a full-file
  read+rewrite under one lock; on own-cloud that is an S3 GET+PUT of the whole
  file. Unbounded growth -> rising per-write cost across all agents.

THE FIX (this helper)
  A store-agnostic bounding primitive that any append-only JSONL can wire into.
  Three modes: line/age bounding (G5/G6/G8/G9/G11) + status compaction (G10):

    mode=cap     drop the oldest entries beyond the bound. ATOMIC (one locked
                 read-modify-write, no second file). For disposable telemetry
                 where old entries carry no recoverable value (gate-firings,
                 presence ticks).
    mode=rotate  MOVE the oldest entries beyond the bound into a sibling
                 <stem>-archive.jsonl, then drop them from the live file. For
                 logs whose history matters (journal, changelog, evolution-log,
                 board). Mirrors the existing experience/aspirations/pipeline
                 archive-sweep family (two-phase locked: append-to-archive then
                 rewrite-live with a fresh in-lock read so concurrent appends
                 are preserved).
    mode=compact STATUS-based physical compaction for active knowledge stores
                 (reasoning-bank/guardrails/pattern-signatures): MOVE records
                 whose status is in {retired,superseded} -- and older than
                 grace_days -- into <stem>-archive.jsonl, then drop them from the
                 live file. Unlike cap/rotate (which act on the OLDEST front
                 slice), compact selects SCATTERED non-active records by status,
                 so retrieval-eligible (active) records always stay. Age-grace
                 (ts_field, default `created`; guardrails use retirement_date)
                 preserves the recent-retire / guardrail_retire.restore undo
                 window. Archive-FIRST + drop-with-status-reverify: a record
                 un-retired between snapshot and lock is kept (recoverable
                 archive dup, never a live loss). Same locked_modify_jsonl path.

    by=lines     bound = max_lines newest records kept.
    by=age       bound = records with <ts_field> within retention_days kept;
                 older records dropped (cap) or archived (rotate).

  COMPACT scope (g-333-10 / G10): the mode=compact path above is the keystone
  extension for the active knowledge stores. It is status-aware (not oldest-N)
  and age-graced, because those stores hold live retrievable knowledge whose
  retirement carries a restore/audit window (guardrail_retire.restore reads
  retired records from the live file; the age-grace keeps the recent-retire
  window intact). Dropped records remain recoverable from <stem>-archive.jsonl
  + the .history snapshot. valid_to is unused across these stores today, so no
  live bitemporal reader depends on retired records staying in the live file.

SAFETY
  - Every write routes through _fileops.locked_modify_jsonl -> cross-machine
    lock + force-fresh-from-backend read + If-Match fenced PUT + .history
    snapshot + post-write JSONL canary + surrogate gate. Same path the daemon
    uses. Dropped records in `rotate` mode are recoverable from the archive
    file AND from the .history snapshot.
  - `rotate` is archive-FIRST (append to archive, then drop from live). A crash
    in the window leaves the moved records in BOTH files (a recoverable archive
    duplicate), never lost. The live-drop modifier re-verifies the oldest n
    records are unchanged before dropping; if the front shifted unexpectedly it
    ABORTS the live rewrite (no drop), so a surprise can only leave an archive
    dup, never a live loss.
  - `cap` is a single atomic locked rewrite (keep newest), so there is no
    two-file window at all.
  - DRY-RUN by default. --apply performs writes.
  - No-op below threshold: a store within its bound is never touched.

USAGE
  Single store (dry-run):
    MIND_AGENT=<a> py -3 core/scripts/jsonl_hygiene.py rotate \
      --path agents/<a>/journal.jsonl --mode rotate --by lines --max-lines 5000
  Apply:
    ... --apply
  Sweep the registry (core/config/store-hygiene.yaml):
    MIND_AGENT=<a> py -3 core/scripts/jsonl_hygiene.py sweep --apply
  Report stores stuck over cap across consecutive runs (never writes a store):
    MIND_AGENT=<a> py -3 core/scripts/jsonl_hygiene.py detect-overcap
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import (  # noqa: E402
    PROJECT_ROOT,
    WORLD_DIR,
    META_DIR,
    AGENT_DIR,
    agent_dir,
    agents_root,
)

DEFAULT_ARCHIVE_SUFFIX = "-archive"
REGISTRY_REL = "core/config/store-hygiene.yaml"

# : hot-store rotation lock-contention retry. world/changelog.jsonl is
# the system's HOTTEST store -- every locked world write by all 6 agents appends
# to it -- so the rotate Phase-2 live-drop's non-fair 10s lock acquire
# (_fileops.acquire_lock default timeout) can lose the race to rapid short
# appends and raise TimeoutError. Observed 2026-06-27: two consecutive Phase-2
# failures on world/changelog.jsonl while the SAME store rotated cleanly the
# prior day -- the contention is PROBABILISTIC and self-healing (the next sweep
# usually wins the race; the live/archive partition stays clean, no dup/loss).
# Retry-with-backoff gives the live-lock acquire several fresh windows to catch a
# quiescent gap, converting "fails this sweep, waits 24h for the next" into
# "almost always succeeds this sweep". Tunable; cold stores acquire on attempt 0
# so they pay no retry cost. See world/knowledge tree (changelog-rotation) + the
#  investigation.
_ROTATE_LIVE_LOCK_RETRIES = 4          # total Phase-2 live-drop acquire attempts
_ROTATE_LIVE_LOCK_BASE_DELAY = 0.5     # seconds; backoff = base * 2**attempt + jitter


# ---------------------------------------------------------------------------
# Path resolution: virtual prefixes -> external roots. Mirrors the world/meta
# virtual-prefix convention (path-resolution.md). A registry entry path may be:
#   world/...            -> WORLD_DIR/...
#   meta/...             -> META_DIR/...
#   agents/<name>/...    -> <that agent's dir>/...
#   agents/*/...         -> glob across every agent dir
#   <absolute or other>  -> used as-is (relative to cwd)
# A '*' anywhere triggers glob expansion AFTER prefix resolution.
# ---------------------------------------------------------------------------
def _resolve_paths(virtual: str) -> list[Path]:
    v = str(virtual).strip().replace("\\", "/")
    base: Path | None = None
    rest = v
    if v.startswith("world/"):
        base, rest = (Path(WORLD_DIR) if WORLD_DIR else None), v[len("world/"):]
    elif v.startswith("meta/"):
        base, rest = (Path(META_DIR) if META_DIR else None), v[len("meta/"):]
    elif v.startswith("agents/"):
        tail = v[len("agents/"):]
        name, _, sub = tail.partition("/")
        if name == "*":
            # Expand across all agent dirs, then apply the (possibly globbed) sub.
            out: list[Path] = []
            root = agents_root()
            if root is None:
                return []
            for ad in sorted(Path(root).glob("*")):
                if ad.is_dir() and (ad / "local-paths.conf").exists():
                    out.extend(_glob_or_single(ad / sub))
            return out
        base, rest = (Path(agent_dir(name)) if name else None), sub
    else:
        # Absolute or cwd-relative path, used verbatim.
        return _glob_or_single(Path(v))
    if base is None:
        return []
    return _glob_or_single(base / rest)


def _glob_or_single(p: Path) -> list[Path]:
    s = str(p)
    if "*" in s:
        # Path.glob needs a base + pattern split at the first '*' segment.
        parts = Path(s).parts
        for i, seg in enumerate(parts):
            if "*" in seg:
                base = Path(*parts[:i]) if i else Path(".")
                pattern = str(Path(*parts[i:]))
                # INVARIANT: a glob must NEVER match an archive sink. Rotation
                # MOVES the oldest records INTO <stem>-archive<suffix>; if the
                # same glob (e.g. world/board/*.jsonl) re-matched that sink, the
                # next sweep would rotate it into a <stem>-archive-archive sink --
                # an unbounded archive-of-archive chain (, observed
                # 2026-06-26: coordination-archive-archive.jsonl). DEFAULT_ARCHIVE_SUFFIX
                # is the sole archive marker (also used by _default_archive), so
                # excluding it here keeps rotation from ever rotating the thing it
                # rotates INTO. An EXPLICIT single-path target (no '*') still
                # passes archives through, so a deliberate age-cap of one archive
                # remains possible.
                return [m for m in sorted(base.glob(pattern))
                        if DEFAULT_ARCHIVE_SUFFIX not in m.stem]
        return []
    return [p]


# ---------------------------------------------------------------------------
# Reading / timestamp extraction
# ---------------------------------------------------------------------------
def _snapshot(path: Path) -> list[dict]:
    """Force-fresh-from-backend then parse the store with the SAME reader
    locked_modify_jsonl uses inside its lock (read_jsonl_with_recovery, via
    _fresh_read.read_jsonl_fresh).
    Reading consistently is load-bearing: the rotate live-drop modifier verifies
    its fresh in-lock read's front equals this snapshot's front, so any
    divergence in how malformed lines are handled would spuriously abort the
    rotate. Empty list if absent."""
    from _fresh_read import read_jsonl_fresh
    return read_jsonl_fresh(path, label="jsonl-hygiene")


def _ts(rec: dict, ts_field: str):
    """Parse a record's timestamp field to a datetime, or None."""
    raw = rec.get(ts_field)
    if not raw:
        return None
    s = str(raw)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:10], fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Candidate selection (pure): which records are "old" / over-bound.
# Returns (n_drop, reason) where the n_drop OLDEST records are the candidates.
# For by=lines the oldest are the front slice; for by=age the oldest are the
# leading contiguous run with ts < cutoff (append-only => old records are at the
# front, so a front-count is exact for both policies).
# ---------------------------------------------------------------------------
def _select(items: list[dict], *, by: str, max_lines=None,
            retention_days=None, ts_field=None):
    n = len(items)
    if by == "lines":
        if max_lines is None:
            raise ValueError("by=lines requires max_lines")
        if n <= max_lines:
            return 0, f"within bound ({n} <= {max_lines})"
        return n - max_lines, f"{n} > {max_lines} (drop {n - max_lines} oldest)"
    if by == "age":
        if retention_days is None or ts_field is None:
            raise ValueError("by=age requires retention_days and ts_field")
        cutoff = datetime.now().replace(microsecond=0) - timedelta(days=retention_days)
        # Count the leading run of records older than cutoff. Stop at the first
        # record that is within retention (or has no parseable ts -> keep, to be
        # conservative: never drop an undateable record on age policy).
        n_drop = 0
        for rec in items:
            dt = _ts(rec, ts_field)
            if dt is not None and dt < cutoff:
                n_drop += 1
            else:
                break
        if n_drop == 0:
            return 0, f"none older than {cutoff.isoformat()}"
        return n_drop, f"{n_drop} older than {cutoff.isoformat()} ({retention_days}d)"
    raise ValueError(f"unknown by={by!r} (expected lines|age)")


def _select_compact(items: list[dict], *, status_field="status",
                    retired_values=("retired", "superseded"),
                    grace_days=None, ts_field=None, today=None):
    """Select non-active records eligible for status-based physical compaction.

    Eligible = status in `retired_values` AND (no grace OR age >= grace_days).
    Age is read from `ts_field`, falling back to `created` when absent on a
    record (guardrails carry retirement_date; reasoning-bank / pattern-signatures
    have only created/valid_from). A record whose age is unparseable is
    CONSERVATIVELY KEPT (never archived on an undateable record), mirroring the
    by=age cap policy. Active (retrieval-eligible) records are never selected.
    Returns (eligible_records, reason).
    """
    retired = set(retired_values)
    today = today or datetime.now().replace(microsecond=0)
    cutoff = None if not grace_days or int(grace_days) <= 0 else \
        today - timedelta(days=int(grace_days))
    eligible = []
    n_nonactive = 0
    for rec in items:
        if rec.get(status_field) not in retired:
            continue
        n_nonactive += 1
        if cutoff is None:
            eligible.append(rec)
            continue
        dt = _ts(rec, ts_field) if ts_field else None
        if dt is None:
            dt = _ts(rec, "created")
        if dt is None:
            continue  # conservative: never archive an undateable retired record
        if dt < cutoff:
            eligible.append(rec)
    if n_nonactive == 0:
        reason = f"no records with {status_field} in {sorted(retired)}"
    elif not eligible:
        reason = f"{n_nonactive} non-active, none older than grace ({grace_days}d)"
    else:
        reason = (f"{len(eligible)} of {n_nonactive} non-active eligible "
                  f"(>= {grace_days}d old)" if cutoff else
                  f"{len(eligible)} non-active (no grace)")
    return eligible, reason


def _default_archive(path: Path) -> Path:
    return path.with_name(path.stem + DEFAULT_ARCHIVE_SUFFIX + path.suffix)


# ---------------------------------------------------------------------------
def _recovery_layer_absent(path: Path):
    """(absent, reason) — does a bare record-dropping rewrite of `path` have NO recovery layer?

    `cap` mode writes no archive sibling, so its ONLY recovery layer is the
    .history snapshot that locked_modify_jsonl takes before overwriting. That
    snapshot is SKIPPED for every store on the _fileops snapshot blacklist,
    whose stated rationale is "the file IS the history" -- true for an
    append-only READER, and false the moment a cap sweep truncates the file.
    Measured 2026-09-04 (g-115-8978): 30,588 records dropped across three
    blacklisted stores with archive=None and no snapshot -- unrecoverable.

    Fail-CLOSED by contract (archive-before-delete.md step 2): when the
    recovery layer cannot be VERIFIED it is treated as ABSENT. A store outside
    every governed root gets no snapshot either, so an unresolvable base dir is
    a true absence, not a conservative guess. The cost of a wrong refusal is a
    store that stays unbounded until someone reads the reason; the cost of a
    wrong apply is permanent record loss.
    """
    try:
        from _fileops import resolve_base_dir, _is_snapshot_blacklisted
    except Exception as e:  # pragma: no cover - import shape is pinned by tests
        return True, f"cannot verify .history recovery layer ({e!r})"
    base_dir = resolve_base_dir(path)
    if base_dir is None:
        # DELIBERATELY NARROW, and not an oversight. A path under no governed
        # root gets no snapshot either -- but every store in the measured
        # population (the store-hygiene registry sweep) is governed, while the
        # non-governed callers of hygiene_one are ad-hoc utilities and test
        # harnesses that own their own archive decision. Refusing there would
        # be a scope-creep regression on callers this goal never measured.
        return False, ""
    try:
        rel = Path(path).resolve().relative_to(Path(base_dir).resolve())
    except ValueError as e:
        return True, f"cannot resolve store path against its base dir ({e})"
    if _is_snapshot_blacklisted(base_dir, rel):
        return True, (f"{rel} is snapshot-blacklisted in _fileops, so no "
                      ".history snapshot is taken before the rewrite")
    return False, ""


# The one-store operation. Returns a report dict.
# ---------------------------------------------------------------------------
def hygiene_one(path: Path, *, mode: str, by: str, max_lines=None,
                retention_days=None, ts_field=None, archive_path=None,
                status_field="status", retired_values=("retired", "superseded"),
                grace_days=None, apply: bool = False) -> dict:
    rep = {"path": str(path), "mode": mode, "by": by, "applied": False,
           "dropped": 0, "kept": None, "archive": None, "action": "none"}
    if mode not in ("cap", "rotate", "compact"):
        rep["error"] = f"unknown mode {mode!r} (expected cap|rotate|compact)"
        return rep

    items = _snapshot(path)
    total = len(items)
    rep["total"] = total
    if total == 0:
        rep["action"] = "absent-or-empty"
        return rep

    # mode=compact: status-based scattered selection ( / G10). A distinct
    # path from the oldest-front-slice cap/rotate below — it moves status in
    # {retired,superseded} records (older than grace_days) to the archive, so
    # active retrieval-eligible records always stay.
    if mode == "compact":
        eligible, reason = _select_compact(
            items, status_field=status_field, retired_values=retired_values,
            grace_days=grace_days, ts_field=ts_field)
        rep["reason"] = reason
        rep["non_active"] = sum(
            1 for r in items if r.get(status_field) in set(retired_values))
        if not eligible:
            rep["action"] = "within-bound"
            rep["kept"] = total
            return rep
        archive = Path(archive_path) if archive_path else _default_archive(path)
        rep["archive"] = str(archive)
        rep["dropped"] = len(eligible)
        rep["kept"] = total - len(eligible)
        if not apply:
            rep["action"] = "would-compact"
            return rep
        from _fileops import locked_modify_jsonl
        # Phase 1: archive-FIRST (locked; a crash before Phase 2 leaves a
        # recoverable dup in the archive, never a live loss).
        locked_modify_jsonl(archive, lambda arch: (arch or []) + eligible)
        # Phase 2: drop from live, re-verifying each target is STILL non-active in
        # the fresh in-lock read. A record un-retired between snapshot and lock is
        # KEPT (leaves a recoverable archive dup, never a live loss). Concurrent
        # appends (new active records) survive untouched.
        drop_ids = {r.get("id") for r in eligible if r.get("id")}
        retired_set = set(retired_values)

        def _drop_compacted(fresh):
            return [r for r in fresh
                    if not (r.get("id") in drop_ids
                            and r.get(status_field) in retired_set)]
        locked_modify_jsonl(path, _drop_compacted)
        rep["action"] = "compacted"
        rep["applied"] = True
        return rep

    n_drop, reason = _select(items, by=by, max_lines=max_lines,
                             retention_days=retention_days, ts_field=ts_field)
    rep["reason"] = reason
    if n_drop <= 0:
        rep["action"] = "within-bound"
        rep["kept"] = total
        return rep

    oldest = items[:n_drop]  # append-only => oldest are the front slice
    rep["dropped"] = n_drop
    rep["kept"] = total - n_drop

    if mode == "rotate":
        archive = Path(archive_path) if archive_path else _default_archive(path)
        rep["archive"] = str(archive)

    if not apply:
        # The dry run must predict what apply WILL do, not what it would do if
        # the recovery-layer gate below () did not exist. Reporting
        # `would-cap` for a store whose cap is refused on every apply is the
        # guard-1802 shape -- an audit whose predicate is WIDER than the acting
        # gate's -- and it sends the reader to wait on a fix that never runs.
        # Measured 2026-09-06: world/presence/<agent>.jsonl sat at 30.1x its
        # bound while BOTH this dry run and detect_overcap reported `would-cap`,
        # and every apply returned refused-no-recovery-layer.
        #
        # `dropped`/`kept` are DELIBERATELY left at their would-be values rather
        # than mirrored from the apply branch's (0, total): detect_overcap
        # derives the over-cap ratio as total/kept (_overcap_ratio), so zeroing
        # the drop here would yield a ratio of exactly 1.0 and drop the store
        # out of the over-cap report -- hiding the very store this surfaces.
        if mode == "cap":
            _no_recovery, _why = _recovery_layer_absent(path)
            if _no_recovery:
                rep["action"] = "refused-no-recovery-layer"
                rep["refused_reason"] = (
                    f"{_why}; a cap here would drop {n_drop} record(s) with no "
                    f"archive and no snapshot, so apply WILL refuse. Use "
                    f"mode=rotate (archive-FIRST) to bound this store instead."
                )
                return rep
        rep["action"] = ("would-cap" if mode == "cap" else "would-rotate")
        return rep

    from _fileops import locked_modify_jsonl

    if mode == "cap":
        # RECOVERY-LAYER GATE (). cap writes NO archive sibling, so
        # the .history snapshot is the only thing standing between this
        # rewrite and permanent loss. Verify it BEFORE the destructive write --
        # there is no "after": the drop IS the locked write, so a post-hoc
        # check would report the loss it was meant to prevent.
        # archive-before-delete.md step 5: only then delete.
        _no_recovery, _why = _recovery_layer_absent(path)
        if _no_recovery:
            rep["action"] = "refused-no-recovery-layer"
            rep["applied"] = False
            rep["dropped"] = 0
            rep["kept"] = total
            rep["refused_reason"] = (
                f"{_why}; cap would drop {n_drop} record(s) with no archive "
                f"and no snapshot. Use mode=rotate (archive-FIRST) to bound "
                f"this store instead."
            )
            return rep
        # Atomic: keep the newest (total - n_drop). Using a fresh in-lock read,
        # keep the LAST `keep` records. Concurrent appends (newest) are kept.
        # Retry-with-backoff mirrors rotate Phase-2 (): world/changelog
        # and meta/changelog are both hot stores whose lock-acquire can time out
        # under rapid concurrent appends. Without retry the cap silently fails
        # (action=error) and the store stays unbounded until the next sweep.
        keep = total - n_drop
        _cap_keep = keep  # capture for lambda closure
        _cap_last_timeout = None
        for _cap_attempt in range(_ROTATE_LIVE_LOCK_RETRIES):
            try:
                locked_modify_jsonl(
                    path,
                    lambda fresh: fresh[-_cap_keep:] if len(fresh) > _cap_keep else fresh,
                )
                _cap_last_timeout = None
                break
            except TimeoutError as e:
                _cap_last_timeout = e
                if _cap_attempt < _ROTATE_LIVE_LOCK_RETRIES - 1:
                    time.sleep(_ROTATE_LIVE_LOCK_BASE_DELAY * (2 ** _cap_attempt)
                               + random.uniform(0, 0.25))
        if _cap_last_timeout is not None:
            raise _cap_last_timeout
        rep["action"] = "capped"
        rep["applied"] = True
        rep["live_lock_attempts"] = _cap_attempt + 1
        return rep

    # mode == rotate: archive-FIRST, then drop from live.
    archive = Path(archive_path) if archive_path else _default_archive(path)
    # Phase 1: append the oldest n_drop to the archive (locked; creates if absent).
    # IDEMPOTENT SINCE . A Phase-2 live-drop that exhausts its lock
    # retries re-raises (:below) with the slice STILL IN THE LIVE FILE, so the
    # next sweep re-derives the SAME `oldest` front slice and, before this
    # guard, appended it to the archive a second time -- a record archived
    # twice while it sat in the live file ONCE. (That is also why guard-1816's
    # action_hint (e) overstates its tell: "a record can only be archived twice
    # if it was in the live file twice" is false across two sweeps.)
    # The skip matches on the CANONICAL SERIALIZED FORM -- the same key Phase 2
    # uses below -- and never on id: an id match would keep a stale copy
    # (guard-3092). It is anchored at the archive's TAIL and takes the MAXIMAL
    # overlap, so a re-run after a failed Phase 2 is a no-op append, while a
    # re-run whose n_drop has since GROWN appends only the new remainder.
    # Not a general dedup: it cannot and must not collapse duplicates already
    # sitting in the archive (guard-2941 -- a writer-side dedup that touches
    # only one twin is inert on a dirty store). Existing residue is a separate,
    # deliberately-untouched population; see this goal's outcome note.
    oldest_keys = [json.dumps(rec, sort_keys=True) for rec in oldest]
    archive_skipped = [0]

    def _append_new_tail(arch):
        arch = arch or []
        window = min(len(oldest_keys), len(arch))
        overlap = 0
        # TWO-TIER, not a descending scan over every k ( occurrence 52,
        # zeta, hostname cc-02, uname -r 6.8.0-139-generic, 2026-09-19). The old
        # `for k in range(window, 0, -1)` slice-compared at every k, so it was
        # O(window**2) with the COMMON case -- overlap 0 -- as its WORST case,
        # because nothing breaks early. Measured here: world/changelog.jsonl at
        # 387,652 lines against its 20,000 cap gave window=367,652 and ~6.8e10
        # comparisons; the sweep burned 35 min at 99% CPU / 1.48 GB RSS inside ONE
        # locked_modify_jsonl cycle. MEASURED: three py-spy dumps 30 s apart all
        # landed on the slice-compare line, and the sweep had to be SIGTERMed
        # (rc=143) having produced zero output. NOT measured, but structural: that
        # cycle's lock TTL is 30 s (acquire_lock stale_seconds), so a 35-min cycle
        # is certainly holding a stale-broken lock well before it writes, which
        # exposes it to a lost If-Match fence and a full _rmw_with_conflict_retry
        # re-run of the whole quadratic pass. No retry was observed here -- the
        # exposure is the hazard, the quadratic is the defect. Either way the shape
        # is a ratchet: the deeper a store drifts past its cap the larger n_drop,
        # so the sweep that would drain it is the one least able to finish.
        # Semantics are UNCHANGED: the same maximal overlap is returned
        # (pinned by test_failed_phase2_then_clean_rerun_archives_each_record_once,
        # archive_skipped 6 then 0); only provably-impossible k values are skipped.
        if window:
            # Tier 1 -- an overlap of k ends AT the archive's last record, so
            # arch[-1] must equal one of oldest[:window]. One serialization plus a
            # set membership test settles overlap==0 without serializing the
            # archive tail at all.
            last_key = json.dumps(arch[-1], sort_keys=True)
            if last_key in set(oldest_keys[:window]):
                # Tier 2 -- only offsets where the tail record equals oldest[0] can
                # begin an overlap. i ascending IS k descending (k = window - i), so
                # the first confirmed match is still the MAXIMAL one.
                tail_keys = [json.dumps(r, sort_keys=True) for r in arch[-window:]]
                first = oldest_keys[0]
                for i, t in enumerate(tail_keys):
                    if t == first and tail_keys[i:] == oldest_keys[:window - i]:
                        overlap = window - i
                        break
        archive_skipped[0] = overlap
        return arch + oldest[overlap:]

    locked_modify_jsonl(archive, _append_new_tail)
    # Phase 2: drop from the fresh in-lock read ONLY the leading records Phase 1
    # archived, matched as parsed records in canonical form (). Never
    # compare BYTES against the outside-lock snapshot: the backend re-fetch can
    # serialize differently (OwnCloud eventual-consistency, cache lag), which is
    # why the old byte-equality check raised spuriously. Never drop by COUNT
    # either: nothing serializes rotators (sweep holds no lease), so when another
    # rotation dropped the front after this snapshot was taken, the fresh front
    # starts past that cut and a count removes records NO rotation archived --
    # measured loss, test_a_rotation_on_a_stale_snapshot_drops_only_what_it_archived.
    # A front another rotation already removed makes this a partial or empty
    # drop; the archive keeps a recoverable duplicate, never a live loss.
    archived = {}
    for rec in oldest:
        key = json.dumps(rec, sort_keys=True)
        archived[key] = archived.get(key, 0) + 1

    # `dropped` stays the archived count; `live_dropped` is what actually left
    # the live file, which is smaller when another rotation got there first
    # (guard-2603). Last write wins: a 412 re-run of the body is the one persisted.
    live_cut = [0]

    def _drop_front(fresh):
        left = dict(archived)
        cut = 0
        for rec in fresh:
            key = json.dumps(rec, sort_keys=True)
            if left.get(key, 0) <= 0:
                break
            left[key] -= 1
            cut += 1
        live_cut[0] = cut
        return fresh[cut:]
    # Phase 2 live-drop, with hot-store lock-contention retry (). Catch
    # ONLY TimeoutError (the acquire_lock failure: "Could not acquire lock").
    # Phase 1 (archive append) is NOT re-run on retry, so a busy window never
    # appends a duplicate orphan to the archive; archive-FIRST crash-safety holds.
    last_timeout = None
    for _attempt in range(_ROTATE_LIVE_LOCK_RETRIES):
        try:
            locked_modify_jsonl(path, _drop_front)
            last_timeout = None
            break
        except TimeoutError as e:
            last_timeout = e
            if _attempt < _ROTATE_LIVE_LOCK_RETRIES - 1:
                time.sleep(_ROTATE_LIVE_LOCK_BASE_DELAY * (2 ** _attempt)
                           + random.uniform(0, 0.25))
    if last_timeout is not None:
        # Exhausted every window -- identical failure surface to pre-:
        # the sweep's per-store try/except reports action=error and the next
        # sweep retries. The archive holds a recoverable front-slice
        # (archive-FIRST); the live file is untouched (no loss).
        raise last_timeout
    rep["action"] = "rotated"
    rep["applied"] = True
    rep["live_dropped"] = live_cut[0]
    rep["live_lock_attempts"] = _attempt + 1
    # Non-zero means Phase 1 recognised its own prior append at the archive tail
    # and skipped it -- i.e. this sweep is retrying a rotation whose Phase 2
    # failed. It is the observable for the idempotency above; a rotation that
    # silently did the right thing is otherwise indistinguishable from one that
    # never needed to.
    rep["archive_skipped"] = archive_skipped[0]
    return rep


# ---------------------------------------------------------------------------
# Registry sweep
# ---------------------------------------------------------------------------
def _load_registry() -> dict:
    reg_path = Path(PROJECT_ROOT) / REGISTRY_REL
    if not reg_path.exists():
        return {"version": 1, "defaults": {}, "stores": []}
    import yaml
    data = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {"version": 1, "defaults": {}, "stores": []}
    data.setdefault("defaults", {})
    data.setdefault("stores", [])
    if data["stores"] is None:
        data["stores"] = []
    return data


def sweep(apply: bool = False) -> dict:
    reg = _load_registry()
    defaults = reg.get("defaults") or {}
    reports = []
    for entry in reg.get("stores") or []:
        if not isinstance(entry, dict):
            continue
        cfg = {**defaults, **entry}
        if not cfg.get("enabled", False):
            reports.append({"path": entry.get("path"), "action": "disabled"})
            continue
        vpath = cfg.get("path")
        if not vpath:
            continue
        resolved = _resolve_paths(vpath)
        if not resolved:
            reports.append({"path": vpath, "action": "unresolved"})
            continue
        for p in resolved:
            try:
                rep = hygiene_one(
                    p,
                    mode=cfg.get("mode", "rotate"),
                    by=cfg.get("by", "lines"),
                    max_lines=cfg.get("max_lines"),
                    retention_days=cfg.get("retention_days"),
                    ts_field=cfg.get("ts_field"),
                    archive_path=cfg.get("archive_path"),
                    status_field=cfg.get("status_field", "status"),
                    retired_values=tuple(
                        cfg.get("retired_values", ("retired", "superseded"))),
                    grace_days=cfg.get("grace_days"),
                    apply=apply,
                )
                rep["owner_goal"] = cfg.get("owner_goal")
                reports.append(rep)
            except Exception as e:  # noqa: BLE001 - one store must not abort the sweep
                reports.append({"path": str(p), "action": "error", "error": str(e)})
    return {"swept": len(reports), "apply": apply, "reports": reports}


# ---------------------------------------------------------------------------
# Over-cap detector ()
# ---------------------------------------------------------------------------
# `sweep()` already computes an action and a live-vs-bound size for every store
# on every run, and nothing consumed them -- so a store sitting far over its cap
# was visible only to whoever happened to read a dry-run by hand. Measured on one
# box: 37 line-capped stores, one of them at 19.98x its cap and another at 2.68x,
# with a third store of the SAME kind sitting at 1.00x in the same sweep. This
# surfaces that for free (learning-philosophy.md: know asap if something is
# wrong).
#
# THE STATE-FILE NAME IS LOAD-BEARING -- do not rename it. Consecutive-run state
# is a PER-BOX fact: several capped stores are machine-local, so one box's sweep
# says nothing about another's, and a SYNCED state file would let every box
# overwrite every other box's history. `world/*-log.jsonl` is ALREADY classified
# machine-local by owncloud_sync's
# `prefix == "world" and fnmatch(basename, "*-log.jsonl")` rule, so this basename
# gets per-box status with no sync-policy change. Verified against the
# sync-candidacy SSOT `owncloud_sync.refresh_would_clobber` -- which unions
# _EXCLUDE_DIRS with _is_machine_local, neither predicate alone being the answer
# -- in BOTH directions: this basename classifies machine-local, and the same
# name WITHOUT the `-log` suffix classifies synced.
#
# The log SELF-TRUNCATES to the last OVERCAP_LOG_KEEP runs. An unbounded state
# file inside a store-hygiene detector would be an instance of the defect it
# detects.
OVERCAP_LOG_REL = "store-hygiene-overcap-log.jsonl"
# 1.25, lowered from 2.0 (). A rotated store only drifts over its cap
# while the sweep that bounds it is not landing, and 2.0 waited for a whole
# second cap of growth: 8 days for coordination (613 posts/day on 5000), ~24 for
# findings. On a 16h sweep cadence the shared rotated stores sit at 1.02-1.15x
# between sweeps, so 1.25 still reads a healthy cadence as quiet. Measured on
# one box before the change: the stores over 1.25x were the same four as over
# 2.0x (all machine-local, 7.9-38x), so the new default flags nothing new today.
OVERCAP_THRESHOLD = 1.25
OVERCAP_LOG_KEEP = 20
# Ratchet (). Four machine-local stores sit permanently over cap on an
# unswept box -- meta/history-save-telemetry 38.2x, world/changelog 18.7x,
# world/presence/<agent> 11.6x, meta/changelog 7.9x -- because per-box
# enforcement is sequenced behind , not because a sweep is falling
# behind. Wired to a clock without a ratchet, this detector would name those
# four on EVERY run forever. An alarm that cannot be driven quiet is one readers
# learn to skip, and the newly-starved SYNCED store it exists to catch would
# arrive buried in a list that never changes (guard-6508). rb-8533 states the
# split this implements: fixable-danger detectors hard-alarm, unfixable-debt
# detectors ratchet.
#
# A store SURFACES once when it first becomes a repeat offender, then goes quiet
# at the ratio it surfaced at. It surfaces AGAIN only when it regresses past that
# level by this factor, and its ratchet entry is DROPPED the moment it falls back
# under threshold -- so the alarm can clear and can then fire again, which is the
# property guard-6508 asks a detector to demonstrate rather than assert.
#
# 1.5 is chosen against the measured spread, not picked round: the synced stores
# ride 1.02-1.15x between sweeps (a 13% band), so a factor inside that band would
# re-fire on ordinary sweep jitter once a store had surfaced. 1.5 sits well clear
# of it while still catching the doubling-class growth that made this goal: the
# coordination board object reached ~2.5x its at-cap size before rotation.
OVERCAP_REGRESS_FACTOR = 1.5


def _overcap_ratio(rep: dict):
    """Live records as a multiple of the store's configured line bound, or None.

    Only a `by: lines` store has a bound to be a multiple OF. For `by: age`,
    `kept` is whatever fell inside the retention window, so total/kept there
    measures CHURN, not over-cap -- returning a ratio for it would report a
    busy store as an unbounded one. `mode` is deliberately not filtered:
    cap/rotate/compact all bound by lines when `by == "lines"`.

    When a line-bounded store is under its cap, `kept == total` and the ratio is
    exactly 1.0, so the same expression covers both sides without a branch.
    """
    if rep.get("by") != "lines":
        return None
    kept = rep.get("kept")
    total = rep.get("total")
    if not isinstance(kept, int) or not isinstance(total, int) or kept <= 0:
        return None
    return total / kept


def _machine_local(path) -> tuple:
    """(is_machine_local, error) for one store path -- never raises.

    Routed through `owncloud_sync.refresh_would_clobber`, the sync-candidacy
    SSOT, rather than `_is_machine_local` alone: the latter returns False for
    directory-excluded paths that ARE per-box, so calling it directly
    misclassifies exactly the stores this detector cares most about.

    On any failure this returns (None, reason) -- NEVER False. An unreadable
    classifier is unknown, not "synced"; collapsing it to a bool would be a
    confident answer manufactured from an error (verify-before-assuming rule 4).
    """
    try:
        from storage_backend import get_backend
        import owncloud_sync
        be = get_backend()
        return bool(owncloud_sync.refresh_would_clobber(be, Path(path))), None
    except Exception as e:  # noqa: BLE001 - classification is enrichment, not the verdict
        return None, f"{type(e).__name__}: {e}"


def _overcap_log_path() -> Path:
    return Path(WORLD_DIR) / OVERCAP_LOG_REL


def _read_prev_overcap():
    """The most recent recorded run, or None if there is no readable one."""
    p = _overcap_log_path()
    if not p.exists():
        return None
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            return rec
    return None


def _append_overcap_run(entry: dict) -> None:
    from _fileops import locked_modify_jsonl

    def _fn(cur):
        rows = list(cur or [])
        rows.append(entry)
        return rows[-OVERCAP_LOG_KEEP:]

    locked_modify_jsonl(_overcap_log_path(), _fn)


def detect_overcap(threshold: float = OVERCAP_THRESHOLD, record: bool = True) -> dict:
    """Report line-bounded stores at or past `threshold` x their cap.

    FIRES only on a store that was over-threshold on the PREVIOUS recorded run
    too -- a single over-cap reading is the normal state of a store between
    sweeps, so one reading is a level and two consecutive readings are a store
    the sweep is not bringing back down.

    AND only on a store the RATCHET has not already surfaced (g-358-115). A
    repeat offender surfaces once, at the ratio it surfaced at, then goes quiet;
    it surfaces again only on regressing past that mark by
    OVERCAP_REGRESS_FACTOR, and loses its mark entirely once it falls back under
    threshold. Read `fired` with `surfaced` (what is new or worse) beside
    `ratcheted_quiet` (known debt being carried) -- `repeat_offenders` is still
    the raw consecutive-run set and is NOT the alarm.

    Always DRY-RUN: `sweep(apply=False)`. This measures; it never rotates.

    Two limits a reader must carry:

    * `machine_local: false` means the same store exists on every box, so its
      ratio here is one box's reading of a shared object -- and under an
      own-cloud backend the local tree is a read-through cache, so a stale local
      copy can under- or over-state it (guard-3992). `machine_local: true`
      stores are authoritative by construction, and `null` means the classifier
      itself was unreadable.
    * `line_bounded` is printed beside `over_now` deliberately: an over-cap
      count means nothing without the population it came from (guard-2273).
    """
    res = sweep(apply=False)
    reports = res.get("reports") or []

    ratios = {}
    for rep in reports:
        r = _overcap_ratio(rep)
        if r is None:
            continue
        ratios[str(rep.get("path"))] = {
            "ratio": round(r, 4),
            "total": rep.get("total"),
            "bound": rep.get("kept"),
            "mode": rep.get("mode"),
            "action": rep.get("action"),
            "owner_goal": rep.get("owner_goal"),
        }

    over_now = {}
    for path, info in sorted(ratios.items()):
        if info["ratio"] >= threshold:
            ml, ml_err = _machine_local(path)
            row = dict(info)
            row["machine_local"] = ml
            if ml_err:
                row["machine_local_error"] = ml_err
            over_now[path] = row

    prev = _read_prev_overcap()
    prev_over = sorted((prev or {}).get("over") or {})
    repeat = sorted(p for p in over_now if p in set(prev_over))

    # RATCHET (). Composed WITH the consecutive-run rule above, not in
    # place of it: only a repeat offender is eligible to surface at all, so a
    # single over-cap reading between sweeps is still silent. Among the
    # eligible, a store surfaces when it is NEW to the ratchet or has regressed
    # past the level it last surfaced at; otherwise it is carried quietly.
    prev_ratchet = dict((prev or {}).get("ratchet") or {})
    eligible = set(repeat)
    newly_over, regressed, carried = [], [], []
    for path in sorted(eligible):
        now_ratio = over_now[path]["ratio"]
        marked = prev_ratchet.get(path)
        if not isinstance(marked, (int, float)):
            newly_over.append(path)
        elif now_ratio >= marked * OVERCAP_REGRESS_FACTOR:
            regressed.append(path)
        else:
            carried.append(path)
    surfaced = sorted(newly_over + regressed)
    # A store that fell back under threshold loses its mark entirely, so a later
    # recurrence surfaces again. This is the half that lets the alarm CLEAR.
    ratchet_cleared = sorted(p for p in prev_ratchet if p not in over_now)
    next_ratchet = {p: over_now[p]["ratio"] for p in surfaced}
    for path in carried:
        # Track the mark DOWN as well as up (). A verbatim carry made
        # this a HIGH-WATER latch: a store that improved while staying over
        # threshold kept its worst-ever fire line, so it could degrade back to
        # that level -- and past it -- in silence. The CLEAR path below only
        # rescues a store that falls fully under threshold, which the four
        # permanently-over-cap machine-local stores never do.
        #
        # min() is safe here because the oscillation was MEASURED, not assumed:
        # N=20 recorded runs (2026-09-18T21:25 .. 2026-09-20T17:01) give a
        # per-store band of 1.041-1.142x with a worst single-run step of 1.011x,
        # across all seven over-cap stores. OVERCAP_REGRESS_FACTOR is 1.5, so an
        # upswing off the trough cannot reach the fire line on ordinary jitter.
        #
        # This is NOT the guard-6519 shape. That guardrail's precondition is a
        # baseline FLEET-MERGED BY MIN, and this log has no merge handler at all:
        # merge_handler_for("world/store-hygiene-overcap-log.jsonl") is None,
        # with meta/audit-baselines.yaml -> merge_audit_baselines as the positive
        # control in the same lookup. The mark is per-box state in a per-box log.
        next_ratchet[path] = min(prev_ratchet[path], over_now[path]["ratio"])

    out = {
        "threshold": threshold,
        "swept": res.get("swept"),          # every store the registry resolved
        "line_bounded": len(ratios),        # the population the ratio is defined over
        "over_now": over_now,
        "over_prev": prev_over,
        "prev_run_at": (prev or {}).get("at"),
        "first_run": prev is None,
        "repeat_offenders": repeat,
        # `fired` is RATCHET-AWARE as of  and is deliberately no longer
        # `bool(repeat_offenders)`. repeat_offenders is retained unchanged beside
        # it as the raw consecutive-run reading, so a reader can still see the
        # full over-cap set that is being carried quietly.
        "surfaced": surfaced,
        "newly_over": sorted(newly_over),
        "regressed": sorted(regressed),
        "ratcheted_quiet": sorted(carried),
        "ratchet_cleared": ratchet_cleared,
        "regress_factor": OVERCAP_REGRESS_FACTOR,
        "fired": bool(surfaced),
        "recorded": False,
    }
    if any(over_now[p].get("machine_local") is not True for p in over_now):
        out["caveat"] = (
            "at least one over-cap store is not machine-local (or was "
            "unclassifiable): its size here is THIS box's reading of a shared "
            "object, which an own-cloud read-through cache can misstate"
        )
    _refused = sorted(p for p in over_now
                      if over_now[p].get("action") == "refused-no-recovery-layer")
    if _refused:
        out["unbounded_by_refusal"] = _refused
        out["unbounded_by_refusal_note"] = (
            "these stores are over cap AND their registered mode=cap is refused "
            "by the recovery-layer gate on every apply, so no sweep can bring "
            "them down -- they stay unbounded until store-hygiene.yaml's mode or "
            "the _fileops snapshot blacklist is reconciled. A repeat_offenders "
            "entry here is NOT a sweep that is falling behind; it is a sweep "
            "that is structurally unable to act."
        )
    if prev is None:
        out["note"] = (
            "first recorded run on this box -- nothing can FIRE yet by design; "
            "the next run has a predecessor to compare against"
        )

    if record:
        try:
            _append_overcap_run({
                "at": datetime.now().isoformat(timespec="seconds"),
                "threshold": threshold,
                "swept": out["swept"],
                "line_bounded": out["line_bounded"],
                "over": {p: over_now[p]["ratio"] for p in over_now},
                "fired": out["fired"],
                "repeat_offenders": repeat,
                # Carried forward on EVERY recorded run, so the self-truncation
                # to the last OVERCAP_LOG_KEEP runs can never drop the ratchet:
                # the newest record always holds the whole current mark set.
                "ratchet": next_ratchet,
            })
            out["recorded"] = True
        except Exception as e:  # noqa: BLE001 - a state-write failure must not lose the reading
            out["record_error"] = f"{type(e).__name__}: {e}"

    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("rotate", help="bound a single store (mode chosen by --mode)")
    r.add_argument("--path", required=True,
                   help="store path (virtual world/|meta/|agents/<a>/ prefix, "
                        "absolute, or cwd-relative; may contain a glob)")
    r.add_argument("--mode", choices=("cap", "rotate", "compact"), default="rotate")
    r.add_argument("--by", choices=("lines", "age"), default="lines")
    r.add_argument("--max-lines", type=int, default=None)
    r.add_argument("--retention-days", type=int, default=None)
    r.add_argument("--ts-field", default=None,
                   help="record field holding the ISO timestamp (by=age, or "
                        "compact age-grace; falls back to `created`)")
    r.add_argument("--archive-path", default=None,
                   help="override archive file (rotate/compact mode; default <stem>-archive<suffix>)")
    r.add_argument("--grace-days", type=int, default=None,
                   help="compact mode: only archive non-active records older than "
                        "this many days (default: no grace)")
    r.add_argument("--status-field", default="status",
                   help="compact mode: record field naming the lifecycle status")
    r.add_argument("--retired-values", default="retired,superseded",
                   help="compact mode: comma-separated status values to archive")
    r.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")

    s = sub.add_parser("sweep", help="bound every enabled store in store-hygiene.yaml")
    s.add_argument("--apply", action="store_true", help="perform writes (default: dry-run)")

    d = sub.add_parser(
        "detect-overcap",
        help="report line-bounded stores at/past N x their cap on consecutive runs")
    d.add_argument("--threshold", type=float, default=OVERCAP_THRESHOLD,
                   help=f"multiple of the cap that counts as over (default {OVERCAP_THRESHOLD})")
    d.add_argument("--no-record", action="store_true",
                   help="do not append this run to the per-box state log "
                        "(a probe; leaves the consecutive-run comparison untouched)")
    d.add_argument("--exit-on-hits", action="store_true",
                   help="exit 1 when the detector fires (default: report-only, exit 0)")

    args = ap.parse_args()

    if args.cmd == "detect-overcap":
        result = detect_overcap(threshold=args.threshold, record=not args.no_record)
        print(json.dumps(result, indent=2, default=str))
        return 1 if (result["fired"] and args.exit_on_hits) else 0

    if args.cmd == "sweep":
        result = sweep(apply=args.apply)
        print(json.dumps(result, indent=2, default=str))
        return 0

    # cmd == rotate (single store): may expand to several paths via glob.
    paths = _resolve_paths(args.path)
    if not paths:
        print(f"[jsonl-hygiene] no path resolved for {args.path!r}", file=sys.stderr)
        return 2
    out = []
    for p in paths:
        try:
            out.append(hygiene_one(
                p, mode=args.mode, by=args.by, max_lines=args.max_lines,
                retention_days=args.retention_days, ts_field=args.ts_field,
                archive_path=args.archive_path,
                status_field=args.status_field,
                retired_values=tuple(
                    v.strip() for v in args.retired_values.split(",") if v.strip()),
                grace_days=args.grace_days, apply=args.apply))
        except Exception as e:  # noqa: BLE001
            out.append({"path": str(p), "action": "error", "error": str(e)})
    print(json.dumps(out if len(out) > 1 else out[0], indent=2, default=str))
    # Non-zero exit if any store errored, so a wrapper/recurring goal sees failure.
    return 1 if any(o.get("action") == "error" for o in out) else 0


if __name__ == "__main__":
    sys.exit(main())
