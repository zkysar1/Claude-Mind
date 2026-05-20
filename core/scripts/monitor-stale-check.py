#!/usr/bin/env python3
"""Monitor Stale Check — auto-complete superseded Monitor goals.

Scans every Monitor goal across agent + world queues whose title contains a
processor run-ID (`proc-NNNNNNNNNN` or `proc-NNNNNNNNNNNNN`). For each goal
still `pending` or `in-progress`, compares the goal's embedded proc-ID
against the current run_dir reported by `processor-run.sh check-complete`.
If the goal's ID is older (< current) AND the goal's age exceeds
--max-age-hours (default 48), marks it superseded.

Dry-run by default; pass --apply to actually mutate goal status.

Safety gates (fail-closed — a missing prerequisite skips the sweep):
 - No processor-run.sh check-complete → no comparison target → skip entirely
 - run_dir missing from JSON (status == "running" or parse failure) → skip
 - Goal's title does not match the proc-NNN regex → skip that goal

Exit code: always 0 (reporting tool).
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

PROC_ID_RE = re.compile(r"proc-(\d{10,13})")
RUN_DIR_ID_RE = re.compile(r"(\d{10,13})")


def _run(argv, input_text=None):
    r = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


def _py(args, input_text=None):
    """Invoke a core script via the current Python interpreter. See blocker-recheck.py."""
    return _run([sys.executable] + args, input_text=input_text)


def _resolve_world_dir() -> Path:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import _paths  # noqa: WPS433 — local path dep
        return Path(_paths.WORLD_DIR)
    except Exception:
        return Path()


def _normalize_proc_id(digits: str) -> int:
    """proc-IDs come in two widths: 10-digit (seconds) and 13-digit (ms).
    Normalize both to ms so comparisons are valid across widths."""
    n = int(digits)
    if n < 10_000_000_000:
        return n * 1000
    return n


def _get_current_run_id(world_dir: Path) -> tuple:
    """Return (run_dir_basename, normalized_ms) or (None, None) if unavailable.

    Reads MSC_CHECK_COMPLETE_JSON populated by monitor-stale-check.sh rather
    than shelling into bash ourselves (sig-005 bypass).
    """
    raw = os.environ.get("MSC_CHECK_COMPLETE_JSON", "").strip()
    if not raw:
        return None, None
    try:
        data = json.loads(raw.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None, None
    run_dir = data.get("run_dir")
    if not run_dir:
        return None, None
    # run_dir may be "proc-NNN" or raw digits; accept both.
    m = RUN_DIR_ID_RE.search(str(run_dir))
    if not m:
        return run_dir, None
    return run_dir, _normalize_proc_id(m.group(1))


def _load_goals(source: str) -> list:
    """Load active aspirations for a source queue, flattening goals.

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    The daemon's /v1/aspirations/read endpoint requires at least one filter
    flag; we pass active=True since the candidate filter at line 192 only
    matches goals with status pending or in-progress anyway. Closes delta
    finding msg-20260516-102012-delta-1169 (Apply landed by bravo iter 43).
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"monitor-stale-check: {source} read failed: {e.body or e}", file=sys.stderr)
        return []
    if not out or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    flat = []
    for asp in data if isinstance(data, list) else []:
        asp_id = asp.get("id")
        for g in asp.get("goals", []):
            g["_asp_id"] = asp_id
            g["_source"] = source
            flat.append(g)
    return flat


def _age_hours(goal: dict) -> float:
    """Age in hours from goal.created (ISO) to now. -1 if unparseable."""
    created = goal.get("created") or goal.get("createdAt")
    if not created:
        return -1.0
    try:
        t = dt.datetime.fromisoformat(created.rstrip("Z"))
    except ValueError:
        return -1.0
    return (dt.datetime.now() - t).total_seconds() / 3600.0


def _apply_completion(goal: dict, current_run_dir: str) -> tuple:
    """Mark the goal completed via aspirations.py update-goal."""
    reason = f"superseded-by-newer-run ({current_run_dir})"
    rc, out, err = _py(
        [
            str(SCRIPT_DIR / "aspirations.py"),
            "update-goal",
            "--source",
            goal["_source"],
            goal["id"],
            "status",
            "completed",
        ]
    )
    if rc != 0:
        return False, err.strip() or out.strip()
    # Annotate outcome_note for the encoding/journal path.
    _py(
        [
            str(SCRIPT_DIR / "aspirations.py"),
            "update-goal",
            "--source",
            goal["_source"],
            goal["id"],
            "outcome_note",
            reason,
        ]
    )
    return True, reason


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=48.0)
    ap.add_argument("--apply", action="store_true", help="Actually complete goals (default: dry-run)")
    args = ap.parse_args()

    world_dir = _resolve_world_dir()
    if not world_dir or not world_dir.exists():
        print(json.dumps({"skipped": "world_dir_unresolved"}))
        return 0

    current_run_dir, current_ms = _get_current_run_id(world_dir)
    if current_ms is None:
        print(
            json.dumps(
                {
                    "skipped": "no_current_run_id",
                    "run_dir": current_run_dir,
                }
            )
        )
        return 0

    goals = _load_goals("world") + _load_goals("agent")
    candidates = []
    for g in goals:
        if g.get("status") not in ("pending", "in-progress"):
            continue
        title = g.get("title") or ""
        if "Monitor" not in title:
            continue
        m = PROC_ID_RE.search(title)
        if not m:
            continue
        goal_ms = _normalize_proc_id(m.group(1))
        if goal_ms >= current_ms:
            continue
        age_h = _age_hours(g)
        if age_h < args.max_age_hours:
            continue
        candidates.append(
            {
                "goal_id": g["id"],
                "asp_id": g["_asp_id"],
                "source": g["_source"],
                "title": title,
                "status": g.get("status"),
                "age_hours": round(age_h, 1),
                "goal_proc_id": m.group(1),
                "current_run_dir": current_run_dir,
            }
        )

    actions = []
    if args.apply:
        for c in candidates:
            ok, detail = _apply_completion(
                next(
                    g
                    for g in goals
                    if g["id"] == c["goal_id"] and g["_source"] == c["source"]
                ),
                current_run_dir,
            )
            actions.append(
                {
                    "goal_id": c["goal_id"],
                    "completed": ok,
                    "detail": detail,
                }
            )

    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "current_run_dir": current_run_dir,
                "max_age_hours": args.max_age_hours,
                "candidates": candidates,
                "candidate_count": len(candidates),
                "actions_taken": actions,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
