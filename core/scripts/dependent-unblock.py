#!/usr/bin/env python3
"""dependent-unblock — Phase 5 dependent-goal unblocker (rb-428 lineage).

Bash-enforces the LLM-orchestrated "Unblock Dependent Goals" loop in
aspirations-verify/SKILL.md Phase 5. When a predecessor goal completes,
two things must happen across all dependent goals (in either world or
agent aspirations.jsonl):

  1. blocked_by cleanup: remove <completed-goal-id> from each dependent
     goal's blocked_by list. For non-recurring predecessors,
     aspirations.py _clear_stale_blockers already handles this when
     status reaches a terminal value (see aspirations.py line ~1342).
     For recurring predecessors (which never set status=completed,
     guarded at aspirations.py line ~1101) and edge cases where verify
     calls this wrapper before the status update, this script ensures
     the cleanup fires.

  2. Output-summary injection: for each dependent goal whose
     `depends_on` lists this id, prepend
     "## Predecessor Output (<id>)\\n<summary>\\n\\n" to its description
     and stamp `unblocked_by` + `unblocked_summary` fields. The
     "## Predecessor Output (<id>)" marker makes the operation
     idempotent — a second invocation skips already-injected goals.

Replaces the LLM-orchestrated loop in aspirations-verify/SKILL.md Phase 5
("Unblock Dependent Goals (with Output Passing)" section). Verify Phase 5
calls this wrapper instead of orchestrating the loop inline.

Usage:
  bash dependent-unblock.sh --goal <completed-goal-id>
                            --summary "<text>"
                            [--dry-run]

Stdout (JSON):
  {
    "completed_goal_id": "...",
    "summary_chars": N,
    "dry_run": bool,
    "unblocked": [{"goal_id","source","asp_id"}, ...],
    "injected": [{"goal_id","source","asp_id","stamp_ok"}, ...],
    "skipped": [{"goal_id","source","step","reason"}, ...]
  }

Exit codes:
  0 success (zero or more goals updated; per-goal failures land in skipped[])
  1 usage error (missing --goal / --summary, empty summary)
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
from aspirations import read_jsonl  # noqa: E402


def _scan(completed_id):
    """Walk world + agent aspirations.jsonl and locate matches.

    Returns (blocked_matches, dep_matches):
      blocked_matches: list of (source, asp_id, goal) where blocked_by
                       contains completed_id (or equals it as a string).
      dep_matches:     list of (source, asp_id, goal) where depends_on
                       has an entry whose goal_id is completed_id.
    A single goal may appear in both lists when both fields are set,
    which is the typical case per goal-schemas.md "Output-Passing
    Dependencies" rule ("Each `depends_on.goal_id` MUST also appear in
    `blocked_by`").
    """
    blocked = []
    deps = []
    for source, base in (("world", WORLD_DIR), ("agent", AGENT_DIR)):
        if base is None:
            continue
        path = base / "aspirations.jsonl"
        if not path.exists():
            continue
        for asp in read_jsonl(path):
            asp_id = asp.get("id", "")
            for goal in asp.get("goals", []):
                # `or []` not `.get(..., [])` — the default only fires when the
                # KEY IS ABSENT, but a goal may carry `blocked_by: null`
                # explicitly, which returned None and crashed the `in` test.
                bb = goal.get("blocked_by") or []
                if isinstance(bb, str):
                    bb = [bb]
                if completed_id in bb:
                    blocked.append((source, asp_id, goal))
                dlist = goal.get("depends_on", [])
                if isinstance(dlist, list):
                    for d in dlist:
                        did = d.get("goal_id") if isinstance(d, dict) else d
                        if did == completed_id:
                            deps.append((source, asp_id, goal))
                            break
    return blocked, deps


def _update(source, goal_id, field, value, dry_run):
    """Shell out to aspirations-update-goal.sh.

    Single source of truth for goal writes — going through the wrapper
    inherits the lock + archive cross-check + auto-blocked_since
    semantics in aspirations.py cmd_update_goal. Bypassing it would
    re-introduce torn-write risk and the very LLM-orchestration drift
    the rb-428 family is meant to retire.

    Windows path-separator fix (g-115-786): use `.as_posix()` instead of
    `str(...)` for the wrapper path. On Windows, `str(WindowsPath)`
    yields backslash separators (e.g. `C:\\<WORKSPACE>\\...`); when bash
    receives that path through subprocess.run(["bash", ...]), it
    interprets each `\\X` as an escape sequence and collapses backslashes,
    yielding the nonexistent `C:<WORKSPACE>...` path observed in the
    g-115-781 incident report. `.as_posix()` produces forward-slash form
    (`C:/<WORKSPACE>/...`) which bash accepts verbatim. Without this fix
    every dependent-unblock call silently no-ops on Windows: unblocked=[]
    and injected=[] with skipped reasons logging the stripped path.
    """
    if dry_run:
        return True, ""
    from _runtime_bash import BASH  # rb-1472: bin-first, clean-PATH-safe (not bare "bash")
    cmd = [
        BASH,
        (SCRIPT_DIR / "aspirations-update-goal.sh").as_posix(),
        "--source", source,
        goal_id, field, value,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True,
                       encoding="utf-8", errors="replace")
        return True, ""
    except subprocess.CalledProcessError as e:
        err = (e.stderr or "")[:300]
        return False, err
    except FileNotFoundError as e:
        return False, "wrapper not found: " + str(e)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--goal", required=True,
                    help="goal_id of the just-completed predecessor goal")
    ap.add_argument("--summary", required=True,
                    help="output_summary text to inject into dependent goals")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan + report only; perform no writes")
    args = ap.parse_args(argv)

    completed_id = args.goal
    summary = args.summary
    if not summary.strip():
        print("dependent-unblock: --summary must be non-empty",
              file=sys.stderr)
        return 1

    marker = "## Predecessor Output (" + completed_id + ")"
    blocked_matches, dep_matches = _scan(completed_id)

    unblocked = []
    injected = []
    skipped = []

    # Step 1: blocked_by cleanup. Remove completed_id from each match.
    for source, asp_id, goal in blocked_matches:
        gid = goal.get("id", "")
        bb = goal.get("blocked_by", [])
        if isinstance(bb, str):
            bb = [bb]
        new_bb = [b for b in bb if b != completed_id]
        ok, err = _update(source, gid, "blocked_by",
                          json.dumps(new_bb), args.dry_run)
        if ok:
            entry = {"goal_id": gid, "source": source, "asp_id": asp_id}
            # Step 1b (RC1): clearing the LAST blocked_by must also return the
            # goal to `pending`. Before this, we dropped the dependency but left
            # status="blocked" forever — the goal was free but invisible to the
            # selector, and nothing anywhere else performs this transition. The
            # leak is silent and CUMULATIVE: every future unblock stranded its
            # own dependents, so the backlog only ever grew.
            #
            # Three conditions, all required — each one is a goal that must NOT
            # be flipped:
            #   1. not new_bb        — still blocked by OTHER predecessors.
            #   2. status=="blocked" — never touch completed/skipped/in-progress
            #                          /expired; only `blocked` is ours to undo.
            #   3. no blocker_ref    — a structured blocker is a SEPARATE
            #                          suppression axis owned by CREATE_BLOCKER
            #                          and its re-probe sweep. blocked_by going
            #                          empty says nothing about whether THAT
            #                          blocker cleared, so leave it alone.
            # `defer_reason` is deliberately NOT a condition: defer and status
            # are orthogonal axes (a pending+deferred goal is normal and stays
            # suppressed by the defer), and gating on it would re-strand exactly
            # the goals that carry both.
            if (not new_bb
                    and goal.get("status") == "blocked"
                    and not goal.get("blocker_ref")):
                ok2, err2 = _update(source, gid, "status", "pending",
                                    args.dry_run)
                entry["status_restored"] = bool(ok2)
                if not ok2:
                    skipped.append({"goal_id": gid, "source": source,
                                    "step": "status_restore", "reason": err2})
            unblocked.append(entry)
        else:
            skipped.append({"goal_id": gid, "source": source,
                            "step": "blocked_by", "reason": err})

    # Step 2: depends_on output injection. Prepend the marker block to
    # each dependent goal's description, stamp unblocked_by /
    # unblocked_summary. Idempotent via marker check.
    seen = set()
    for source, asp_id, goal in dep_matches:
        gid = goal.get("id", "")
        if gid in seen:
            continue
        seen.add(gid)
        existing_desc = goal.get("description", "") or ""
        if marker in existing_desc:
            skipped.append({"goal_id": gid, "source": source,
                            "step": "inject",
                            "reason": "already-injected"})
            continue
        new_desc = marker + "\n" + summary + "\n\n" + existing_desc
        ok, err = _update(source, gid, "description",
                          new_desc, args.dry_run)
        if not ok:
            skipped.append({"goal_id": gid, "source": source,
                            "step": "description", "reason": err})
            continue
        ok2, _ = _update(source, gid, "unblocked_by",
                         completed_id, args.dry_run)
        ok3, _ = _update(source, gid, "unblocked_summary",
                         summary, args.dry_run)
        injected.append({"goal_id": gid, "source": source,
                         "asp_id": asp_id,
                         "stamp_ok": bool(ok2 and ok3)})

    payload = {
        "completed_goal_id": completed_id,
        "summary_chars": len(summary),
        "dry_run": args.dry_run,
        "unblocked": unblocked,
        "injected": injected,
        "skipped": skipped,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
