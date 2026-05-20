#!/usr/bin/env python3
"""Parent-Goal Supersession Sweep.

Detects parent "Apply: <topic>" goals that have been deferred while one or
more sibling goals in the same aspiration completed work that functionally
satisfies the parent's intent.

Pattern (rb-842, g-268-10 incident):
    Parent g-268-10 "Apply: Implement BT-seed prefetch" sat
    pending+deferred from 2026-05-06 to 2026-05-11 despite siblings
    g-268-15 ("Design: Option C", completed 2026-05-06) and g-268-16
    ("Apply: SeedGetterCache 119 LOC", completed 2026-05-06) fully
    satisfying the parent's verification criteria. The defer_reason
    narrative cited the unblock path but no script detected the sibling
    completion. This sweep closes that gap as a companion to
    blocker-recheck.sh / defer-recheck.sh / monitor-stale-check.sh.

Heuristic (conservative for v1 — favors precision):
    For each candidate parent goal P:
        - title starts with "Apply:"
        - status in (pending, in-progress)
        - defer_reason is non-null
        - age (defer_reason_set_at OR created_at) >= --max-age-hours
    Parent aspiration must be small (--max-aspiration-goals, default 50):
        - Large aspirations (e.g., asp-115 "Recurring Infrastructure
          Monitoring" with 611 goals) accumulate unrelated completions
          that produce false-positive matches. The canonical incident
          (g-268-10 / asp-268) was a 16-goal sprint where sibling
          decomposition genuinely covered parent intent. The size guard
          replicates "sprint-scope cohesion" without requiring topic
          string matching (which would have FAILED to detect the
          canonical incident — "BT-seed prefetch" shares no words with
          "SeedGetterCache").
    Scan sibling goals in same aspiration:
        - status == completed
        - title starts with "Apply:" or "Design:"
        - completed_at (or completed_date) > P.defer_reason_set_at
          (parent must have been deferred BEFORE sibling completion;
           otherwise the parent is the consumer of the sibling, not
           superseded by it)
    If >= --min-siblings (default 2) such siblings: P is a supersession
    candidate.

Action modes:
    --report (default): print JSON {candidates: [...], details: [...]}
    --apply: mark each candidate parent as status=completed with
        outcome_note "superseded by sibling decomposition: <sibling-ids>".
        Idempotent: if the parent already has outcome_note containing
        "superseded by sibling decomposition", skip the rewrite.

Eligibility filters (mirror defer-recheck.py):
    - status MUST be pending OR in-progress (not completed/skipped/blocked)
    - deferred_until set: SKIPPED (structured time gate is authoritative)
    - aspiration must be active (archived aspirations are out of scope)

Companion scripts (rb-428 family):
    - blocker-recheck.py — Layer C for participants:[user] blockers
    - defer-recheck.py — explicit dep-chain narrative defers
    - precondition-defer-recheck.py — structured precondition_unmet
    - monitor-stale-check.py — Monitor proc-NNN goals past current run_dir
    - pending-questions-sweep.py — source_goal-completed lifecycle

Exit: always 0 (reporting tool, fail-open). Returns JSON:
    {
      "scanned": N,           # total parent-pattern candidates examined
      "eligible": N,          # passed age + defer + status filters
      "candidates": [...],    # supersession candidates (recommend close)
      "applied": N,           # parents marked superseded (apply mode only)
      "details": [...],       # per-goal trace incl. matched siblings
    }

CLI:
    parent-supersession-sweep.py [--max-age-hours N] [--apply] [--output json|human]
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

from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402


APPLY_PATTERN = re.compile(r"^\s*Apply\s*:\s*(.+)$", re.IGNORECASE)
DESIGN_PATTERN = re.compile(r"^\s*Design\s*:\s*(.+)$", re.IGNORECASE)


def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. Mirrors defer-recheck convention."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "parent-supersession-sweep-metrics.jsonl"


def _append_metric(path, record):
    """Append one metric record. Fail-open by design — metric-write
    failure must not abort the sweep."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[parent-supersession-sweep] WARN: metrics append failed: {e}",
              file=sys.stderr)


def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Mirrors consolidation-health.py::_tolerant_decode (lines 112-172,
    landed via g-115-796) adapted for parent-supersession-sweep's expected
    aggregate shapes: dict with "aspirations" key OR bare list.

    Contract per g-115-797-A4 / guard-383 / rb-774:
      - Empty / whitespace-only body: return [] (valid empty queue).
      - Valid JSON (list OR dict with "aspirations" key): return as-is.
      - Valid prefix + trailing garbage (g-115-766 shape): raw_decode
        returns the prefix; recovery is NOT a source error.
      - JSONDecodeError: ONE stderr diagnostic + sys.exit(1).
      - Non-dict-and-non-list aggregate (e.g. string, number): stderr +
        sys.exit(1).
    guard-383 mandates source errors are FATAL for the N>=2 aggregator
    pattern (_read_aspirations is called once for "world" and once for
    "agent" then merged at line 279); returning [] on corruption would
    poison the merged aggregate with a complete-looking lie.
    """
    stripped = (raw or "").lstrip()
    if not stripped:
        return None  # genuinely empty queue — valid state, not source error
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"parent-supersession-sweep: {source} JSONDecodeError ({exc}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: source error is fatal
    if not isinstance(obj, (dict, list)):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"parent-supersession-sweep: {source} non-dict-and-non-list aggregate "
            f"(type={type(obj).__name__}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: corrupt aggregate shape
    return obj


def _read_aspirations(source):
    """Return list of (aspiration_dict, source_str) tuples. Preserves
    aspiration grouping so sibling lookup stays cheap (in-memory, not
    by-id index).

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    Parse path is g-115-766-tolerant via _tolerant_decode — see that
    helper for the contract.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        # guard-383: source error is FATAL for the N>=2-source aggregator
        # pattern (line 279 merges "world" + "agent"). A silent [] would
        # poison the merged aggregate with a complete-looking lie.
        print(f"parent-supersession-sweep: {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    aspirations = data.get("aspirations") if isinstance(data, dict) else data
    return [(asp, source) for asp in (aspirations or [])]


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", ""))
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _parse_ts(ts):
    """Parse ISO timestamp returning datetime or None."""
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", ""))
    except Exception:
        return None


def _is_apply_goal(g):
    return bool(APPLY_PATTERN.match(g.get("title", "") or ""))


def _is_design_or_apply(g):
    title = g.get("title", "") or ""
    return bool(APPLY_PATTERN.match(title) or DESIGN_PATTERN.match(title))


def _find_superseding_siblings(parent, siblings):
    """Return list of sibling goal-ids that plausibly superseded parent.

    A sibling qualifies when:
      - id != parent.id
      - title starts with "Apply:" or "Design:"
      - status == "completed"
      - completed timestamp is AFTER parent's defer_set_at
        (or parent's created_at as fallback)
    """
    parent_defer_set = (parent.get("defer_reason_set_at")
                        or parent.get("created_at"))
    parent_ref_ts = _parse_ts(parent_defer_set)
    if parent_ref_ts is None:
        # Without a parent reference timestamp we cannot enforce the
        # "sibling completed AFTER parent deferred" temporal guard.
        # Skip the goal — conservative.
        return []
    out = []
    for s in siblings:
        if s.get("id") == parent.get("id"):
            continue
        if s.get("status") != "completed":
            continue
        if not _is_design_or_apply(s):
            continue
        s_ts = _parse_ts(s.get("completed_at") or s.get("completed_date"))
        if s_ts is None:
            continue
        if s_ts <= parent_ref_ts:
            continue
        out.append({
            "id": s.get("id"),
            "title": s.get("title", ""),
            "completed_at": s.get("completed_at") or s.get("completed_date"),
        })
    return out


def _mark_superseded(source, goal_id, sibling_ids):
    """Mark parent as completed with outcome_note. Returns True on success.

    INVARIANT: uses sys.executable directly. Same rationale as
    defer-recheck._clear_defer — bash on Windows can resolve to WSL
    bash.exe with surprising PATH semantics; aspirations-update-goal.sh
    just shells aspirations.py with the same args, so calling .py
    directly loses nothing functional."""
    sibling_str = ", ".join(sibling_ids)
    note = f"superseded by sibling decomposition: {sibling_str}"
    # First write outcome_note (informational only — does NOT close goal).
    rc1, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                     "--source", source, "update-goal",
                     goal_id, "outcome_note", note])
    if rc1 != 0:
        return False
    # Then close the goal.
    rc2, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                     "--source", source, "update-goal",
                     goal_id, "status", "completed"])
    if rc2 != 0:
        return False
    # Stamp completed_date if absent.
    today = dt.date.today().isoformat()
    rc3, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                     "--source", source, "update-goal",
                     goal_id, "completed_date", today])
    # completed_date rewrite is best-effort — non-fatal if it fails
    # (most stores stamp it server-side via update-goal hooks).
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-hours", type=float, default=24.0,
                    help="Minimum parent age before consideration (default 24h)")
    ap.add_argument("--min-siblings", type=int, default=2,
                    help="Minimum sibling completion count for candidacy (default 2)")
    ap.add_argument("--max-aspiration-goals", type=int, default=50,
                    help=("Skip aspirations larger than this many goals "
                          "(default 50). Sprint-scope cohesion filter — large "
                          "aspirations like asp-115 accumulate unrelated "
                          "completions that produce false-positive matches."))
    ap.add_argument("--apply", action="store_true",
                    help="Mark candidates as superseded (default: report only)")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/parent-supersession-sweep-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()

    metrics_path = _resolve_metrics_log(args.metrics_log)

    # Gather all aspirations from both sources.
    all_aspirations = _read_aspirations("world") + _read_aspirations("agent")

    scanned = 0
    eligible = 0
    candidates = []
    applied = 0
    details = []

    for asp, source in all_aspirations:
        if asp.get("status") and asp["status"] != "active":
            continue
        goals = asp.get("goals", []) or []
        # Aspiration-size guard (sprint-scope cohesion filter). Large
        # aspirations accumulate unrelated completions that produce
        # false-positive matches. See docstring "Heuristic" section.
        if len(goals) > args.max_aspiration_goals:
            continue
        # Index siblings once per aspiration (cheap).
        for g in goals:
            if not _is_apply_goal(g):
                continue
            scanned += 1
            if g.get("status") not in ("pending", "in-progress"):
                continue
            if not g.get("defer_reason"):
                continue
            if g.get("deferred_until"):
                # Structured time gate is authoritative — same skip rule
                # as defer-recheck.py.
                continue
            age_h = _age_hours(g.get("defer_reason_set_at")
                               or g.get("started")
                               or g.get("created_at"))
            if age_h is None or age_h < args.max_age_hours:
                continue
            eligible += 1
            siblings_match = _find_superseding_siblings(g, goals)
            if len(siblings_match) < args.min_siblings:
                details.append({
                    "goal_id": g.get("id"),
                    "aspiration_id": asp.get("id"),
                    "age_hours": round(age_h, 1),
                    "matched_siblings": len(siblings_match),
                    "action": "skipped",
                    "reason": (f"insufficient sibling matches "
                               f"({len(siblings_match)} < {args.min_siblings})"),
                })
                continue
            sibling_ids = [s["id"] for s in siblings_match]
            entry = {
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "source": source,
                "age_hours": round(age_h, 1),
                "title": g.get("title", ""),
                "defer_reason": g.get("defer_reason", "")[:120],
                "siblings": siblings_match,
                "action": "would_mark",
            }
            candidates.append({
                "goal_id": g.get("id"),
                "aspiration_id": asp.get("id"),
                "siblings": sibling_ids,
            })
            if args.apply:
                ok = _mark_superseded(source, g.get("id"), sibling_ids)
                entry["action"] = "marked" if ok else "mark_failed"
                if ok:
                    applied += 1
                    _append_metric(metrics_path, {
                        "type": "parent_superseded",
                        "timestamp": dt.datetime.now().isoformat(
                            timespec="seconds"),
                        "goal_id": g.get("id"),
                        "source": source,
                        "aspiration_id": asp.get("id"),
                        "age_hours_at_mark": round(age_h, 2),
                        "sibling_ids": sibling_ids,
                        "sibling_count": len(sibling_ids),
                        "agent": os.environ.get("MIND_AGENT", "") or None,
                    })
            details.append(entry)

    # Always log run summary (whether or not anything fired).
    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "candidates": len(candidates),
        "applied": applied,
        "mode": "apply" if args.apply else "report",
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "candidates": candidates,
        "applied": applied,
        "details": details,
    }

    if args.output == "human":
        print(f"parent-supersession-sweep: scanned={scanned} eligible={eligible} "
              f"candidates={len(candidates)} applied={applied} "
              f"mode={'apply' if args.apply else 'report'}")
        for c in candidates:
            print(f"  {c['goal_id']} → superseded by {', '.join(c['siblings'])}")
    else:
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
