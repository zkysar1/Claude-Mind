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
from aspirations import TERMINAL_GOAL_STATUSES, _normalize_terminal_goal


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
            completed_at_missing = g.get("completed_at") is None
            if defer_state or completed_at_missing:
                count += 1
    return count


def scan_file(path: Path) -> dict:
    """Read-only scan: count anomalies. No lock, no write."""
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
    """Lock + read + normalize + atomic write. Idempotent."""
    if not path.exists():
        return {"path": str(path), "exists": False, "before": 0, "after": 0}

    lock_path = path.with_suffix(".lock")
    try:
        acquire_lock(lock_path)

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
