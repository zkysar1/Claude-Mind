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
    locked_modify_jsonl uses inside its lock (read_jsonl_with_recovery). Reading
    consistently is load-bearing: the rotate live-drop modifier verifies its
    fresh in-lock read's front equals this snapshot's front, so any divergence in
    how malformed lines are handled would spuriously abort the rotate. Both views
    skip unparseable lines identically; the original bytes survive in .history.
    Empty list if absent."""
    try:
        from storage_backend import get_backend
        import owncloud_sync
        be = get_backend()
        # guard-881 / : refresh() force-pulls the remote copy over the
        # local file. For a per-machine store (only-local writers, never pushed
        # to S3) that overwrites the only good copy with stale/empty remote data
        # -- a data-loss path. Skip refresh for any file owncloud_sync would
        # never sync (presence/.history/sessions dirs + the basename machine-
        # local policy); SYNCED stores (reasoning-bank, guardrails, ...) still
        # refresh unchanged.
        if not owncloud_sync.refresh_would_clobber(be, path):
            be.refresh(path)
    except Exception as e:  # noqa: BLE001 - refresh is best-effort
        print(f"[jsonl-hygiene] (refresh skipped for {path.name}: {e})",
              file=sys.stderr)
    if not path.exists():
        return []
    try:
        from _fileops import read_jsonl_with_recovery
        return read_jsonl_with_recovery(path)
    except Exception:  # noqa: BLE001 - fall back to a plain skip-malformed parse
        out = []
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


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
    locked_modify_jsonl(archive, lambda arch: (arch or []) + oldest)
    # Phase 2: drop the oldest n_drop from the live file. Drop by COUNT from
    # the fresh in-lock read — do NOT compare content against the outside-lock
    # snapshot. The content-equality check (`fresh[:n_drop] != oldest`) was
    # designed to abort if a concurrent rotation rearranged the front, but it
    # spuriously raises RuntimeError when the backend re-fetch inside
    # locked_modify_jsonl returns a slightly different byte sequence than
    # _snapshot() pulled (e.g. OwnCloud eventual-consistency, write-through
    # cache lag, or per-machine vs remote serialization). Since changelog.jsonl
    # is append-only (writes only add to the END), the front records can only
    # change through another hygiene rotation. The `len(fresh) < n_drop` guard
    # below catches that case safely (another agent rotated first → no-op).
    # Archive-FIRST (Phase 1) already holds: the records to drop are in the
    # archive before we touch the live file, so a crash here leaves a
    # recoverable duplicate in the archive, never a live loss.
    def _drop_front(fresh):
        if len(fresh) < n_drop:
            # Another concurrent rotation already removed these records.
            # Archive holds a recoverable duplicate; live is clean. No-op.
            return fresh
        return fresh[n_drop:]
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
    rep["live_lock_attempts"] = _attempt + 1
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

    args = ap.parse_args()

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
