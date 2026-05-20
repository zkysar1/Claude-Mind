#!/usr/bin/env python3
"""Structured precondition evaluator — domain-agnostic predicate library.

Used by goal-selector.py to filter goals whose declared preconditions aren't
yet satisfied, and by aspirations-execute pre-claim re-check.

Predicate types:
  file_exists_after    — glob matches exist with mtime >= cutoff
  command_succeeds     — allowlisted bash script returns exit 0
  goal_completed_after — another goal's completion timestamp >= cutoff
  file_check           — glob matches exist (no cutoff) — simple existence check
  metric_threshold     — allowlisted script emits count/int; compared against min/max
  after_time           — wall-clock anchor + delay_seconds elapsed (Monitor goals)

Unknown types, malformed predicates, and evaluator errors all fail closed for
the offending predicate but NEVER crash the caller. The selector/caller
decides what to do with a failed result.
"""

import glob
import json
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# resolve_file_path is REQUIRED for the world/ and meta/ virtual-prefix convention.
# Never substitute `PROJECT_ROOT / path` — world/ and meta/ are external directories
# configured in <agent>/local-paths.conf. See .claude/rules/path-resolution.md.
from _paths import PROJECT_ROOT, WORLD_DIR, resolve_file_path, agent_dir as _agent_dir


CLOCK_SKEW_GRACE_SECONDS = 60
DEFAULT_COMMAND_TIMEOUT = 30
MAX_COMMAND_TIMEOUT = 120
ALLOWED_COMMAND_PREFIXES = ("bash core/scripts/", "bash world/scripts/")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class PredicateResult:
    passed: bool
    type: str
    predicate_id: Optional[str] = None
    observed_value: Any = None
    reason: str = ""
    evaluated_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# after_ref resolution (shared by file_exists_after and goal_completed_after)
# ---------------------------------------------------------------------------

def _to_local_naive(dt: datetime) -> datetime:
    """Convert tz-aware datetime to local naive; pass naive datetimes through."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def resolve_after_ref(after_ref: str) -> Optional[datetime]:
    """Resolve 'git:<sha>' | 'iso:<timestamp>' | 'file:<path>' to a datetime.

    Returns None on any failure — callers must treat None as "predicate fails,
    reason: unresolvable ref" (don't silently pass).
    """
    if not isinstance(after_ref, str) or ":" not in after_ref:
        return None
    kind, _, ref = after_ref.partition(":")
    kind = kind.strip().lower()
    ref = ref.strip()
    if not ref:
        return None

    # Timezone policy: the codebase convention (CLAUDE.md) is naive-local
    # timestamps. Any tz-aware input is converted to local wall-clock time
    # before being returned naive. Do NOT change this without also updating
    # the file-mtime comparison, which is local-naive via datetime.fromtimestamp.
    if kind == "iso":
        try:
            return _to_local_naive(datetime.fromisoformat(ref))
        except (ValueError, TypeError):
            return None

    if kind == "file":
        p = resolve_file_path(ref)
        if not p.exists():
            return None
        return datetime.fromtimestamp(p.stat().st_mtime)

    if kind == "git":
        try:
            out = subprocess.run(
                ["git", "show", "-s", "--format=%cI", ref],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode != 0:
                return None
            iso = out.stdout.strip()
            if not iso:
                return None
            return _to_local_naive(datetime.fromisoformat(iso))
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None

    return None


# ---------------------------------------------------------------------------
# Type handlers — each returns PredicateResult
# ---------------------------------------------------------------------------

def _eval_file_exists_after(p: dict) -> PredicateResult:
    pid = p.get("id")
    path_pattern = p.get("path", "")
    after_ref = p.get("after_ref", "")
    min_count = int(p.get("min_count", 1))

    if not path_pattern or not after_ref:
        return PredicateResult(False, "file_exists_after", pid,
                               reason="missing required field: path and after_ref")

    cutoff = resolve_after_ref(after_ref)
    if cutoff is None:
        return PredicateResult(False, "file_exists_after", pid,
                               reason=f"unresolvable after_ref: {after_ref}")

    grace_cutoff_ts = cutoff.timestamp() - CLOCK_SKEW_GRACE_SECONDS
    # resolve_file_path handles world/, meta/, and absolute paths; glob
    # wildcards in the tail are preserved through Path semantics.
    abs_pattern = str(resolve_file_path(path_pattern))
    matches = glob.glob(abs_pattern)
    fresh = [m for m in matches if os.path.getmtime(m) >= grace_cutoff_ts]

    passed = len(fresh) >= min_count
    return PredicateResult(
        passed=passed,
        type="file_exists_after",
        predicate_id=pid,
        observed_value={"matches": len(matches), "fresh": len(fresh), "cutoff": cutoff.isoformat()},
        reason=("ok" if passed else
                f"found {len(fresh)} fresh of {len(matches)} total (need {min_count})"),
    )


def _eval_command_succeeds(p: dict) -> PredicateResult:
    pid = p.get("id")
    command = p.get("command", "")
    timeout_s = min(int(p.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT)), MAX_COMMAND_TIMEOUT)

    if not command:
        return PredicateResult(False, "command_succeeds", pid, reason="missing command")

    if not any(command.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
        return PredicateResult(False, "command_succeeds", pid,
                               reason=f"command not in allowlist (must start with one of: {ALLOWED_COMMAND_PREFIXES})")

    # shell=True is required on Windows Git Bash: the python3 shim under
    # core/scripts/.python-shim cannot resolve its `py` target via direct
    # CreateProcess (shell=False). cmd.exe intermediation keeps the PATH
    # chain intact. If you switch to shell=False, the shim breaks.
    #
    # Rewrite "bash world/X Y Z" -> "bash <quoted absolute WORLD_DIR/X> Y Z"
    # so WORLD_DIR paths containing spaces don't split under shell tokenisation.
    run_command = command
    if command.startswith("bash world/"):
        tail = command[len("bash world/"):]
        script_rel, _, rest = tail.partition(" ")
        script_abs = str(Path(WORLD_DIR) / script_rel)
        run_command = "bash " + shlex.quote(script_abs) + ((" " + rest) if rest else "")

    try:
        out = subprocess.run(
            run_command,
            shell=True,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=timeout_s,
        )
        passed = out.returncode == 0
        return PredicateResult(
            passed=passed,
            type="command_succeeds",
            predicate_id=pid,
            observed_value=out.returncode,
            reason=("ok" if passed else f"exit {out.returncode}"),
        )
    except subprocess.TimeoutExpired:
        return PredicateResult(False, "command_succeeds", pid,
                               reason=f"timeout after {timeout_s}s")
    except OSError as e:
        return PredicateResult(False, "command_succeeds", pid, reason=str(e))


def _eval_goal_completed_after(p: dict) -> PredicateResult:
    pid = p.get("id")
    goal_id = p.get("goal_id", "")
    after_ref = p.get("after_ref", "")

    if not goal_id or not after_ref:
        return PredicateResult(False, "goal_completed_after", pid,
                               reason="missing required field: goal_id and after_ref")

    cutoff = resolve_after_ref(after_ref)
    if cutoff is None:
        return PredicateResult(False, "goal_completed_after", pid,
                               reason=f"unresolvable after_ref: {after_ref}")

    # Lazy import to avoid startup cost and keep the module self-contained.
    import aspirations as asp_mod

    def scan(path: Path) -> Optional[dict]:
        if not path.exists():
            return None
        items = asp_mod.read_jsonl(path)
        res = asp_mod.find_goal_in_aspirations(items, goal_id)
        return res[2]["goals"][res[1]] if res else None

    search_paths = [asp_mod.LIVE_PATH, asp_mod.ARCHIVE_PATH]
    agent = os.environ.get("MIND_AGENT", "").strip()
    if agent:
        search_paths.append(_agent_dir(agent) / "aspirations.jsonl")

    goal = next((g for g in (scan(p) for p in search_paths) if g is not None), None)
    if goal is None:
        return PredicateResult(False, "goal_completed_after", pid,
                               reason=f"goal {goal_id} not found in live or archive")

    # Non-recurring: completed_date. Recurring: lastAchievedAt. Check both.
    completed_ts_str = goal.get("completed_date") or goal.get("lastAchievedAt")
    if not completed_ts_str:
        return PredicateResult(False, "goal_completed_after", pid,
                               observed_value={"status": goal.get("status")},
                               reason=f"goal {goal_id} has no completion timestamp")

    try:
        completed_ts = _to_local_naive(datetime.fromisoformat(str(completed_ts_str)))
    except (ValueError, TypeError):
        return PredicateResult(False, "goal_completed_after", pid,
                               reason=f"unparseable completion timestamp: {completed_ts_str}")

    passed = completed_ts.timestamp() >= (cutoff.timestamp() - CLOCK_SKEW_GRACE_SECONDS)
    return PredicateResult(
        passed=passed,
        type="goal_completed_after",
        predicate_id=pid,
        observed_value={"completed_at": completed_ts.isoformat(), "cutoff": cutoff.isoformat()},
        reason=("ok" if passed else
                f"completed at {completed_ts.isoformat()}, needs >= {cutoff.isoformat()}"),
    )


def _eval_file_check(p: dict) -> PredicateResult:
    pid = p.get("id")
    path_pattern = p.get("path", "")
    min_count = int(p.get("min_count", 1))
    condition = p.get("condition", "exists")

    if not path_pattern:
        return PredicateResult(False, "file_check", pid, reason="missing required field: path")
    if condition not in ("exists", "not_exists"):
        return PredicateResult(False, "file_check", pid,
                               reason=f"unsupported condition '{condition}' (expected exists|not_exists)")

    abs_pattern = str(resolve_file_path(path_pattern))
    matches = glob.glob(abs_pattern)
    count = len(matches)

    if condition == "exists":
        passed = count >= min_count
        reason = "ok" if passed else f"found {count}, need {min_count}"
    else:  # not_exists
        passed = count == 0
        reason = "ok" if passed else f"found {count} matches (expected 0)"

    return PredicateResult(passed=passed, type="file_check", predicate_id=pid,
                           observed_value={"matches": count}, reason=reason)


def _eval_metric_threshold(p: dict) -> PredicateResult:
    """Run allowlisted script, extract integer count from stdout, compare to min/max.

    Extract modes:
      stdout_int   — parse first integer from stdout (tolerates trailing text)
      json_length  — stdout is a JSON array; observed_value = len(array)
      exit_code    — observed_value = script exit code
    """
    pid = p.get("id")
    command = p.get("command", "")
    extract = p.get("extract", "stdout_int")
    timeout_s = min(int(p.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT)), MAX_COMMAND_TIMEOUT)

    min_val = p.get("min")
    max_val = p.get("max")
    if min_val is None and max_val is None:
        return PredicateResult(False, "metric_threshold", pid,
                               reason="must specify at least one of min, max")
    if not command:
        return PredicateResult(False, "metric_threshold", pid, reason="missing command")
    if not any(command.startswith(prefix) for prefix in ALLOWED_COMMAND_PREFIXES):
        return PredicateResult(False, "metric_threshold", pid,
                               reason=f"command not in allowlist (must start with one of: {ALLOWED_COMMAND_PREFIXES})")
    if extract not in ("stdout_int", "json_length", "exit_code"):
        return PredicateResult(False, "metric_threshold", pid,
                               reason=f"unsupported extract mode '{extract}' (expected stdout_int|json_length|exit_code)")

    run_command = command
    if command.startswith("bash world/"):
        tail = command[len("bash world/"):]
        script_rel, _, rest = tail.partition(" ")
        script_abs = str(Path(WORLD_DIR) / script_rel)
        run_command = "bash " + shlex.quote(script_abs) + ((" " + rest) if rest else "")

    try:
        out = subprocess.run(run_command, shell=True, cwd=str(PROJECT_ROOT),
                             capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return PredicateResult(False, "metric_threshold", pid, reason=f"timeout after {timeout_s}s")
    except OSError as e:
        return PredicateResult(False, "metric_threshold", pid, reason=str(e))

    try:
        stdout_text = out.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        stdout_text = ""

    value: Optional[int] = None
    if extract == "stdout_int":
        import re
        m = re.search(r"-?\d+", stdout_text)
        if m:
            try: value = int(m.group(0))
            except ValueError: value = None
    elif extract == "json_length":
        try:
            parsed = json.loads(stdout_text) if stdout_text else []
            if isinstance(parsed, list):
                value = len(parsed)
            elif isinstance(parsed, dict):
                value = len(parsed)
            else:
                return PredicateResult(False, "metric_threshold", pid,
                                       reason=f"json_length expects array/object, got {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            return PredicateResult(False, "metric_threshold", pid,
                                   reason=f"json_length parse error: {e}")
    elif extract == "exit_code":
        value = out.returncode

    if value is None:
        return PredicateResult(False, "metric_threshold", pid,
                               reason=f"could not extract integer via '{extract}' from stdout")

    checks = []
    if min_val is not None:
        checks.append(value >= int(min_val))
    if max_val is not None:
        checks.append(value <= int(max_val))
    passed = all(checks)

    bounds = []
    if min_val is not None: bounds.append(f">={min_val}")
    if max_val is not None: bounds.append(f"<={max_val}")
    reason = "ok" if passed else f"value {value} failed {' AND '.join(bounds)}"

    return PredicateResult(passed=passed, type="metric_threshold", predicate_id=pid,
                           observed_value=value, reason=reason)


def _eval_after_time(p: dict) -> PredicateResult:
    pid = p.get("id")
    anchor_str = p.get("anchor")
    delay = p.get("delay_seconds")
    if not isinstance(anchor_str, str) or not anchor_str:
        return PredicateResult(False, "after_time", pid, reason="missing anchor (ISO timestamp)")
    if not isinstance(delay, (int, float)) or delay < 0:
        return PredicateResult(False, "after_time", pid, reason="missing or invalid delay_seconds (non-negative number)")
    try:
        anchor = _to_local_naive(datetime.fromisoformat(anchor_str))
    except (ValueError, TypeError) as e:
        return PredicateResult(False, "after_time", pid, reason=f"invalid anchor: {e}")
    now = datetime.now()
    available_at = datetime.fromtimestamp(anchor.timestamp() + float(delay))
    passed = now.timestamp() >= available_at.timestamp() - CLOCK_SKEW_GRACE_SECONDS
    return PredicateResult(passed=passed, type="after_time", predicate_id=pid,
                           observed_value=now.strftime("%Y-%m-%dT%H:%M:%S"),
                           reason=("ok" if passed else
                                   f"available_at={available_at.strftime('%Y-%m-%dT%H:%M:%S')} > now"))


PREDICATE_TYPES: Dict[str, Callable[[dict], PredicateResult]] = {
    "file_exists_after":    _eval_file_exists_after,
    "command_succeeds":     _eval_command_succeeds,
    "goal_completed_after": _eval_goal_completed_after,
    "file_check":           _eval_file_check,
    "metric_threshold":     _eval_metric_threshold,
    "after_time":           _eval_after_time,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def evaluate(predicate: dict) -> PredicateResult:
    """Evaluate one structured precondition. Never raises."""
    if not isinstance(predicate, dict):
        return PredicateResult(False, "invalid", None,
                               reason=f"not a dict: {type(predicate).__name__}")
    ptype = predicate.get("type", "")
    handler = PREDICATE_TYPES.get(ptype)
    if handler is None:
        return PredicateResult(False, ptype or "missing", predicate.get("id"),
                               reason="unknown predicate type")
    try:
        return handler(predicate)
    except Exception as e:
        return PredicateResult(False, ptype, predicate.get("id"),
                               reason=f"evaluator error: {type(e).__name__}: {e}")


def evaluate_all(predicates: List[dict], *, mode: str = "fail_fast",
                 include_skippable: bool = True) -> List[PredicateResult]:
    """Evaluate a list of predicates.

    mode='fail_fast' stops at first failure. mode='all' evaluates every one.
    include_skippable=False drops predicates with selector_skip=True before
    evaluating — used by the goal-selector filter to skip expensive checks.
    """
    results: List[PredicateResult] = []
    for p in predicates:
        if not include_skippable and isinstance(p, dict) and p.get("selector_skip"):
            continue
        r = evaluate(p)
        results.append(r)
        if mode == "fail_fast" and not r.passed:
            break
    return results


# ---------------------------------------------------------------------------
# CLI (debug / SKILL pseudocode helper)
# ---------------------------------------------------------------------------

def _cli_goal(goal_id: str, types_filter: str = "structured") -> int:
    """Evaluate preconditions for a single goal by ID. Prints JSON.

    Lookup order: world live queue, then agent live queue (if bound).
    The archive is intentionally not searched — precondition evaluation
    only makes sense for goals still in play.

    Empty-results contract (g-302-03 — documents the upstream semantics
    that g-302-02's bash-enforced precondition-defer-recheck explicitly
    works around):

        Exit 0 from this CLI means "all evaluated predicates passed
        OR no predicates were evaluated (vacuous truth)". The final
        `return 0 if all(...) else 1` is mathematically correct —
        Python's `all([])` returns True — but semantically ambiguous
        for callers that treat exit 0 as "everything checked out
        and the goal is unblocked".

        Callers that need "at least one predicate evaluated" must
        inspect the JSON `results` array length BEFORE trusting the
        exit code. goal-selector.py demonstrates the correct upstream
        pattern at L981 (`if struct_pcs:` pre-filter), and
        precondition-defer-recheck.py mirrors it (struct_pc_count==0
        → SKIP with reason "no structured preconditions to evaluate
        — defer is free-form, LLM judgment required").

        Phase 0.5b.3 of aspirations-precheck/SKILL.md previously
        relied on this CLI's exit code alone and would false-clear
        free-form defers. g-302-02 replaced that loop with the
        in-process bash-enforced sweep specifically to avoid this
        ambiguity. See `core/scripts/precondition-defer-recheck.py`
        for the canonical pre-filter + evaluate_all + clear pattern.
    """
    import aspirations as asp_mod

    search_paths = [asp_mod.LIVE_PATH]
    agent = os.environ.get("MIND_AGENT", "").strip()
    if agent:
        search_paths.append(_agent_dir(agent) / "aspirations.jsonl")

    for path in search_paths:
        if not Path(path).exists():
            continue
        items = asp_mod.read_jsonl(path)
        res = asp_mod.find_goal_in_aspirations(items, goal_id)
        if not res:
            continue
        goal = res[2]["goals"][res[1]]
        pcs = goal.get("verification", {}).get("preconditions") or []
        if types_filter == "structured":
            pcs = [p for p in pcs if isinstance(p, dict) and "type" in p]
        results = [r.to_dict() for r in evaluate_all(pcs, mode="all")]
        print(json.dumps({"goal_id": goal_id, "results": results}, indent=2, default=str))
        # Vacuous-truth: all([]) == True, so an empty results list returns 0
        # (no structured preconditions to evaluate). Cross-references:
        # goal-selector.py L981 (if struct_pcs: pre-filter) +
        # precondition-defer-recheck.py (empty struct_pcs → SKIP, NOT clear) +
        # aspirations-precheck/SKILL.md Phase 0.5b.3 +  (the bash-
        # enforced sweep that exists specifically to avoid relying on this
        # exit code alone). See _cli_goal docstring for the full contract.
        return 0 if all(r["passed"] for r in results) else 1

    print(json.dumps({"goal_id": goal_id, "error": "not found"}, indent=2))
    return 2


def _cli_predicate(raw_json: str) -> int:
    try:
        p = json.loads(raw_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}))
        return 2
    result = evaluate(p)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0 if result.passed else 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Evaluate structured preconditions.")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--goal", help="Goal ID to evaluate preconditions for")
    grp.add_argument("--predicate", help="One predicate as JSON string")
    ap.add_argument("--types", default="structured",
                    help="structured | all (default: structured)")
    args = ap.parse_args()

    if args.goal:
        sys.exit(_cli_goal(args.goal, args.types))
    else:
        sys.exit(_cli_predicate(args.predicate))


if __name__ == "__main__":
    main()
