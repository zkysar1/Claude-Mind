#!/usr/bin/env python3
"""normalize-terminal-defer.py — one-shot backfill + invariant audit for
well-formed terminal-status goals.

Enforces two coupled invariants on goals whose status is in
{completed, skipped, expired, decomposed, superseded}:
  (1) No residual defer state — defer_reason, defer_reason_set_at,
      deferred_until, blocker_ref, blocked_since all cleared (g-115-660
      cluster, fixed 2026-05-12).
  (2) `completed_at` is set — the transition timestamp must be stamped
      (g-115-661 cluster, fixed 2026-05-12 by zeta).

Both invariants are enforced at every disk write boundary inside
aspirations.py via _normalize_terminal_goal (called from
_write_live_under_lock and the three archive write sites in
cmd_complete / cmd_retire / cmd_archive_sweep). Layer 1 stamps also live
inline at the three direct goal["status"] = "completed" sites.

This script does two things:
  - Default mode: lock + scan + auto-heal + write. Cleared 120 anomalies
    on first run (32 world live + 75 world archive + 12 alpha live + 1
    alpha archive). Idempotent — safe to re-run.
  - --check mode: scan only (no writes, no locks). Counts anomalies of
    BOTH classes. Exits non-zero if any remain. Used as the /verify-learning
    Section TGD regression guard.

Targets all four files for the bound agent: world live + archive, and
agent live + archive (if present).
"""
import argparse
import json
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

from _paths import WORLD_DIR, AGENT_DIR
from _fileops import acquire_lock, release_lock, _atomic_write_with_fallback
from storage_backend import get_backend
from aspirations import TERMINAL_GOAL_STATUSES, _normalize_terminal_goal


def _refresh(path: Path) -> None:
    """Pull the store's current bytes into the local cache before reading it.

    g-358-124. Every target of this script is an eager-pull EXCLUDED store on a
    remote-backed box: `owncloud_sync._EAGER_PULL_EXCLUDE_GLOBS` is
    `("*-archive.jsonl",)` over `_EAGER_PULL_ROOTS = ("world", "meta")`, so an
    archive under those roots is never re-pulled once materialized and a box's
    local copy can lag the store by days (g-358-117 measured a board archive
    2,143 records / 5.7 days stale). A plain `open()` of that path is a raw
    local read of a backend-routed store, which guard-980 / guard-1169 forbid
    for exactly this reason.

    WHAT THIS IS *NOT* FIXING, measured rather than assumed (2026-09-17, alpha,
    cc-04, uname -r 6.8.0-139-generic, remote backend live): a stale-base
    rewrite here was NOT a data-loss path. All four targets carry the
    `merge_aspirations` handler -- resolved through
    `owncloud_backend._coordination_merge_handler`, the function `_put` itself
    calls, never a grep of the handler dict -- so `_put`'s fence-None branch
    dispatches to `_merge_reconcile_put`, which GETs the remote-authoritative
    bytes, unions them with ours, PUTs fenced on the remote version, and writes
    the MERGED bytes back to the local cache. Fixture measurement with the real
    handler: zero aspirations and zero goals lost, against a positive control
    (unfenced overwrite with the same stale body) that lost one whole
    aspiration and two goals. Do NOT delete this call on the strength of that:
    the protection is the merge REGISTRY, not this script, and 4 of the 17
    eager-pull-excluded archives under the two shared roots have no handler at
    all.

    WHAT IT *IS* FIXING is the READ, which stays stale either way, and both
    consequences are detector blindness (guard-6878): `--check` is the
    /verify-learning TGD regression guard and can print PASS while records only
    the store holds carry anomalies; and `normalize_file` gates its write on
    `before > 0` computed locally, so an anomaly living only in the newer remote
    records is never healed -- the merge preserves it UNHEALED.

    Zero cost on the local backend: `LocalBackend.refresh` is a documented no-op
    that reads nothing. Best-effort by design -- a transport fault must not wedge
    a backfill that could previously run against the local copy; the scan then
    reports on whatever is local, exactly as it did before this call existed.
    """
    try:
        get_backend().refresh(path)
    except Exception as e:  # noqa: BLE001 - see docstring: never wedge on transport
        print(f"warning: could not refresh {path} before reading it "
              f"({type(e).__name__}: {e}); scanning the local copy, which may "
              f"lag the store", file=sys.stderr)


def _count_anomalies(aspirations):
    """Count terminal-status goals violating EITHER well-formedness invariant:
       (1) residual defer state (defer_reason/_set_at/deferred_until/blocker_ref/blocked_since)
       (2) missing completed_at timestamp
    Both classes are auto-healed by _normalize_terminal_goal at write time,
    so a non-zero count from this scan implies a write path that bypasses
    the normalizer — i.e. a regression worth investigating.
    """
    count = 0
    for asp in aspirations:
        for g in asp.get("goals", []) or []:
            if g.get("status") not in TERMINAL_GOAL_STATUSES:
                continue
            defer_state = (
                g.get("defer_reason") is not None
                or g.get("defer_reason_set_at") is not None
                or g.get("deferred_until") is not None
                or g.get("blocker_ref") is not None
                or g.get("blocked_since") is not None
            )
            # PRESENCE consumer of completed_at (, 2026-08-30), and the
            # exposure runs OPPOSITE to every other consumer. This sweep exists to
            # find terminal goals MISSING a completed_at stamp; the 2026-08-08
            # backfill STAMPED precisely those rows, so backfill does not inflate
            # this check -- it SILENCES it, and a silenced defect-finder reports
            # clean, which is indistinguishable from a healthy corpus. guard-2613
            # prescribes a completed_by filter for at-risk consumers; it does
            # nothing here. Judge this sweep's zero against the backfill date.
            completed_at_missing = g.get("completed_at") is None
            if defer_state or completed_at_missing:
                count += 1
    return count


def scan_file(path: Path) -> dict:
    """Read-only scan: count anomalies. No lock, no write.

    The refresh below is a READ side effect (it materialises the store's current
    bytes into the local cache) and is deliberate: without it this scan -- the
    /verify-learning TGD regression guard -- counts anomalies in a copy the
    eager pull never refreshes. No lock is taken, matching this function's
    contract; a refresh needs none.
    """
    _refresh(path)
    if not path.exists():
        return {"path": str(path), "exists": False, "before": 0}
    aspirations = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            aspirations.append(json.loads(line))
    return {
        "path": str(path),
        "exists": True,
        "before": _count_anomalies(aspirations),
    }


def normalize_file(path: Path) -> dict:
    """Lock + refresh + read + normalize + atomic write. Idempotent."""
    lock_path = path.with_suffix(".lock")
    try:
        acquire_lock(lock_path)

        # INSIDE the lock, before the read -- the ordering is the whole point.
        # A refresh taken outside it can be overtaken by a peer write between
        # the pull and the open, which is the same stale base with extra steps.
        # It also populates the backend's compare-and-swap fence for this key,
        # so the write below is fenced on the version we actually read instead
        # of on a head fetched at write time (owncloud_backend._put, fence-None
        # branch). The existence check FOLLOWS the refresh rather than preceding
        # it: on a cold box an eager-pull-excluded archive may exist ONLY in the
        # store, and checking first would silently skip the whole file.
        _refresh(path)
        if not path.exists():
            return {"path": str(path), "exists": False, "before": 0, "after": 0}

        aspirations = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                aspirations.append(json.loads(line))

        before = _count_anomalies(aspirations)

        for asp in aspirations:
            for g in asp.get("goals", []) or []:
                _normalize_terminal_goal(g)

        after = _count_anomalies(aspirations)

        wrote = False
        if before > 0:
            def _write(handle):
                for item in aspirations:
                    handle.write(json.dumps(item, ensure_ascii=True) + "\n")
            _atomic_write_with_fallback(
                path, _write,
                fallback_counter_key="normalize_terminal_defer")
            wrote = True

        return {
            "path": str(path),
            "exists": True,
            "before": before,
            "after": after,
            "wrote": wrote,
        }
    finally:
        release_lock(lock_path)


def _targets():
    out = [
        WORLD_DIR / "aspirations.jsonl",
        WORLD_DIR / "aspirations-archive.jsonl",
    ]
    if AGENT_DIR is not None:
        out.append(AGENT_DIR / "aspirations.jsonl")
        out.append(AGENT_DIR / "aspirations-archive.jsonl")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="Scan only (no writes). Print PASS/FAIL verdict and "
                             "exit 1 if any anomaly remains. Used by /verify-learning.")
    args = parser.parse_args()

    if args.check:
        total = 0
        details = []
        for path in _targets():
            r = scan_file(path)
            details.append(r)
            if r.get("exists"):
                total += r["before"]
        if total == 0:
            print("PASS: zero terminal-status goal anomalies — defer state cleared "
                  "AND completed_at stamped on all terminal goals "
                  "(world live+archive, agent live+archive)")
            return 0
        per_file = "; ".join(f"{Path(r['path']).name}={r['before']}"
                             for r in details if r.get("exists") and r["before"] > 0)
        print(f"FAIL: {total} terminal-status goal anomalies — either residual "
              f"defer state OR missing completed_at ({per_file}). "
              f"Run `py -3 core/scripts/normalize-terminal-defer.py` to backfill, "
              f"then investigate which write path bypassed _normalize_terminal_goal.")
        return 1

    results = []
    total_before = 0
    total_after = 0
    for path in _targets():
        result = normalize_file(path)
        results.append(result)
        if result.get("exists"):
            total_before += result["before"]
            total_after += result["after"]

    print(json.dumps({
        "total_anomalies_before": total_before,
        "total_anomalies_after": total_after,
        "files": results,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
