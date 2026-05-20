#!/usr/bin/env python3
"""verify-check-eval.py — Tier 1b declarative verification.

Sister to predicate.py. Evaluates `verification.checks[]` from a goal record
for Phase 5 completion verification. Reuses predicate.py's PREDICATE_TYPES
dispatch table — same allow-list, same timeouts, same path resolution —
because a check that verifies "file exists" uses the exact same mechanism
as a precondition that waits for "file exists".

Plan: ~/.claude/plans/i-had-one-agent-luminous-reddy.md (Tier 1b #1).

Check types supported (identical to predicate.py):
  file_check, file_exists_after, command_succeeds, goal_completed_after,
  metric_threshold

Exit codes:
  0 = all checks passed (or checks empty — legitimate fall-through to Q1/Q2/Q3)
  1 = one or more checks failed
  2 = input error / goal not found

JSON stdout always — summary field drives the SKILL.md decision.
"""

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from _paths import AGENT_DIR, PROJECT_ROOT, agent_dir as _agent_dir  # type: ignore
from _fileops import log_script_decision  # type: ignore

# Reuse the dispatch — do NOT re-implement. verify-check-eval is the sister
# of predicate.py: predicate.py evaluates `verification.preconditions`, this
# script evaluates `verification.checks`. Same allow-list, same timeouts,
# same path resolution, same clock-skew grace. Add new check types to
# predicate.PREDICATE_TYPES, not here.
from predicate import evaluate_all  # type: ignore


def _find_goal(goal_id):
    """Lookup goal across world live, agent live, and archive."""
    import aspirations as asp_mod  # type: ignore

    search_paths = [asp_mod.LIVE_PATH, asp_mod.ARCHIVE_PATH]
    agent = os.environ.get("MIND_AGENT", "").strip()
    if agent:
        search_paths.append(_agent_dir(agent) / "aspirations.jsonl")

    for path in search_paths:
        if not Path(path).exists():
            continue
        items = asp_mod.read_jsonl(path)
        res = asp_mod.find_goal_in_aspirations(items, goal_id)
        if res:
            return res[2]["goals"][res[1]], str(path)
    return None, None


def _extract_checks(goal):
    """Return verification.checks[] with legacy completion_check backfilled."""
    verification = goal.get("verification") or {}
    checks = verification.get("checks") or []

    # Legacy fallback — completion_check maps to checks[0]
    if not checks and goal.get("completion_check"):
        checks = [goal["completion_check"]]

    # Filter to dict entries with a `type` field (structured).
    # String entries (natural-language) fall through to LLM Q1/Q2/Q3.
    structured = [c for c in checks if isinstance(c, dict) and c.get("type")]
    string_checks = [c for c in checks if isinstance(c, str)]
    return structured, string_checks


def cmd_goal(args):
    """Evaluate all structured checks[] for a goal by ID."""
    goal, source_path = _find_goal(args.goal)
    if goal is None:
        result = {
            "goal_id": args.goal,
            "error": "goal not found in world live, agent live, or archive",
            "summary": f"goal {args.goal} not found",
            "flags": ["goal_not_found"],
        }
        print(json.dumps(result, ensure_ascii=False, default=str))
        sys.exit(2)

    structured, string_checks = _extract_checks(goal)

    # No structured checks → the script cannot verify. Fall through to LLM
    # Q1/Q2/Q3. all_passed is null (not true) so a caller reading only that
    # field cannot mistakenly conclude the goal is verified.
    if not structured:
        flags = ["checks_empty"]
        if string_checks:
            flags.append("has_string_checks")
        result = {
            "goal_id": args.goal,
            "goal_title": goal.get("title"),
            "source": source_path,
            "checks_total": 0,
            "all_passed": None,
            "summary": "no structured checks — caller must run LLM Q1/Q2/Q3",
            "flags": flags,
            "results": [],
            "string_checks": string_checks,
        }
        print(json.dumps(result, ensure_ascii=False, default=str))
        sys.exit(0)

    mode = "all" if args.all else "fail_fast"
    results = [r.to_dict() for r in evaluate_all(structured, mode=mode)]
    passed_count = sum(1 for r in results if r["passed"])
    total = len(results)
    all_passed = all(r["passed"] for r in results) and len(results) == len(structured)

    flags = []
    if not all_passed:
        flags.append("checks_failed")
    if string_checks:
        flags.append("has_string_checks")  # caller should also run Q1/Q2/Q3 for these

    summary = (
        f"{passed_count}/{total} structured check(s) passed"
        + (f"; {len(string_checks)} string check(s) require LLM evaluation"
           if string_checks else "")
    )

    result = {
        "goal_id": args.goal,
        "goal_title": goal.get("title"),
        "source": source_path,
        "checks_total": total,
        "checks_passed": passed_count,
        "all_passed": all_passed,
        "summary": summary,
        "flags": flags,
        "results": results,
        "string_checks": string_checks,
        "mode": mode,
    }
    log_script_decision("verify-check-eval", {
        "cmd": "goal",
        "checks_total": result.get("checks_total"),
        "all_passed": all_passed,
        "flags": flags,
    })
    print(json.dumps(result, ensure_ascii=False, default=str))
    sys.exit(0 if all_passed else 1)


def cmd_checks(args):
    """Evaluate a raw JSON array of checks (no goal lookup)."""
    try:
        data = json.loads(args.checks)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}", "flags": ["input_error"]}))
        sys.exit(2)
    if not isinstance(data, list):
        print(json.dumps({"error": "--checks must be a JSON array",
                          "flags": ["input_error"]}))
        sys.exit(2)

    structured = [c for c in data if isinstance(c, dict) and c.get("type")]
    string_checks = [c for c in data if isinstance(c, str)]

    if not structured:
        # all_passed=None — script cannot verify. Caller runs LLM Q1/Q2/Q3.
        flags = ["checks_empty"]
        if string_checks:
            flags.append("has_string_checks")
        log_script_decision("verify-check-eval", {
            "cmd": "checks",
            "checks_total": 0,
            "all_passed": None,
            "flags": flags,
        })
        print(json.dumps({
            "checks_total": 0,
            "all_passed": None,
            "summary": "no structured checks — caller must run LLM Q1/Q2/Q3",
            "flags": flags,
            "results": [],
            "string_checks": string_checks,
        }, ensure_ascii=False, default=str))
        sys.exit(0)

    mode = "all" if args.all else "fail_fast"
    results = [r.to_dict() for r in evaluate_all(structured, mode=mode)]
    passed_count = sum(1 for r in results if r["passed"])
    total = len(results)
    all_passed = passed_count == total

    flags = []
    if not all_passed:
        flags.append("checks_failed")
    if string_checks:
        flags.append("has_string_checks")

    log_script_decision("verify-check-eval", {
        "cmd": "checks",
        "checks_total": total,
        "checks_passed": passed_count,
        "all_passed": all_passed,
        "flags": flags,
    })
    print(json.dumps({
        "checks_total": total,
        "checks_passed": passed_count,
        "all_passed": all_passed,
        "summary": f"{passed_count}/{total} structured check(s) passed",
        "flags": flags,
        "results": results,
        "string_checks": string_checks,
        "mode": mode,
    }, ensure_ascii=False, default=str))
    sys.exit(0 if all_passed else 1)


def main():
    ap = argparse.ArgumentParser(description="Evaluate verification.checks[] for a goal")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--goal", help="Goal ID to evaluate checks for")
    grp.add_argument("--checks", help="Raw JSON array of checks (no goal lookup)")
    ap.add_argument("--all", action="store_true",
                    help="Evaluate every check (default: fail-fast on first failure)")
    args = ap.parse_args()

    if args.goal:
        cmd_goal(args)
    else:
        cmd_checks(args)


if __name__ == "__main__":
    main()
