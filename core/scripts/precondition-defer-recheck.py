#!/usr/bin/env python3
"""Precondition Defer Recheck — re-evaluate structured-precondition defers.

For each goal whose defer_reason starts with 'precondition_unmet:', re-run
predicate.evaluate_all against the goal's structured preconditions. When ALL
structured predicates pass, clear defer_reason + defer_reason_set_at.

Replaces the LLM-side loop in aspirations-precheck Phase 0.5b.3 (which called
predicate-eval.sh per-goal as a subprocess and would false-clear when zero
structured predicates existed — the CLI exit-code-0-on-empty "vacuous-truth"
bug). Following the bash-consolidation pattern (rb-428) used by sibling
rechecks: blocker-recheck.sh (Layer C), defer-recheck.sh (Layer C for free-
form defers), monitor-stale-check.sh (proc-ID staleness), pending-questions-
sweep.sh (sentinel lifecycle).

Pre-filter (mirrors goal-selector.py L967-981 — "SYMMETRY: must be the
logical complement of the struct_pc check"):

    struct_pcs = [p for p in (goal.verification.preconditions or [])
                  if isinstance(p, dict) and "type" in p]

When the pre-filtered list is EMPTY, the defer is free-form (string-only
preconditions or no preconditions at all — LLM judgment territory).
Action: SKIP. Do NOT clear. Clearing here was the vacuous-truth bug we are
explicitly avoiding.

When the pre-filtered list is non-empty, evaluate via predicate.evaluate_all
in-process (not a subprocess). In-process avoids the CLI-exit-code-0-on-empty
collision AND avoids the Windows bash subprocess unreliability documented in
defer-recheck.py _clear_defer (sys.executable direct, never shell out).

Dry-run by default; --apply actually clears via aspirations.py update-goal.

Exit: always 0 (reporting tool). JSON output:
  {
    "scanned": N,
    "eligible": N,            # defer_reason set + precondition_unmet: prefix + age >= threshold
    "evaluated": N,           # had structured preconditions to evaluate
    "skipped_free_form": N,   # had no structured preconditions (free-form defer)
    "cleared": N,             # actually cleared (apply only)
    "would_clear": [goal_ids],# dry-run list
    "details": [...],         # per-goal reasoning
    "apply": bool,
    "metrics_log": path|null,
  }

Reference: g-302-02 (this script). Sibling pattern: defer-recheck.py.
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

# Canonical path + fileop primitives (same SSOT as defer-recheck.py).
from _paths import WORLD_DIR  # noqa: E402
from _fileops import locked_append_jsonl  # noqa: E402
from predicate import evaluate_all  # noqa: E402


# --- Metrics logging (mirrors defer-recheck.py — rb-468 family) -------------

def _resolve_metrics_log(cli_path):
    """Resolve metrics log path. cli_path=='' → disabled; None → default."""
    if cli_path == "":
        return None
    if cli_path is not None:
        return Path(cli_path)
    return Path(WORLD_DIR) / "precondition-defer-recheck-metrics.jsonl"


def _append_metric(path, record):
    """Append one record to metrics JSONL. Fail-open: write failures stay
    visible on stderr but never abort the clearance run."""
    if path is None:
        return
    try:
        locked_append_jsonl(str(path), record)
    except Exception as e:
        print(f"[precondition-defer-recheck] WARN: metrics append failed: {e}",
              file=sys.stderr)


# --- Subprocess helpers (mirror defer-recheck.py — sys.executable direct) ---

def _run(argv, input_text=None):
    result = subprocess.run(argv, input=input_text, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    return result.returncode, result.stdout, result.stderr


def _py(args, input_text=None):
    return _run([sys.executable] + args, input_text=input_text)


def _tolerant_decode(source, raw):
    """-tolerant decode for daemon aspirations_read body.

    Mirrors consolidation-health.py::_tolerant_decode (lines 112-172,
    landed via g-115-796) and parent-supersession-sweep.py::_tolerant_decode
    (sibling A4) adapted for precondition-defer-recheck's expected aggregate
    shapes: dict with "aspirations" key OR bare list.

    Contract per g-115-797-A3 / guard-383 / rb-774:
      - Empty / whitespace-only body: return None (caller maps None → [];
        valid empty queue, NOT a source error).
      - Valid JSON (list OR dict with "aspirations" key): return as-is.
      - Valid prefix + trailing garbage (g-115-766 shape): raw_decode
        returns the prefix; recovery is NOT a source error.
      - JSONDecodeError: ONE stderr diagnostic + sys.exit(1).
      - Non-dict-and-non-list aggregate (e.g. string, number): stderr +
        sys.exit(1).

    Pre-fix behavior at line 114 was a bare json.loads + silent
    `except json.JSONDecodeError: return []` — collapsing corruption to
    "no goals to recheck" and freezing precondition_unmet defers
    indefinitely. The fail-open boundary is the caller's shell wrapper
    `|| echo WARN`, never inside this aggregator (rb-347).
    """
    stripped = (raw or "").lstrip()
    if not stripped:
        return None  # genuinely empty queue — valid state, not source error
    try:
        obj, _consumed = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as exc:
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"precondition-defer-recheck: {source} JSONDecodeError ({exc}); "
            f"body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: source error is fatal
    if not isinstance(obj, (dict, list)):
        body_prefix = stripped[:120].replace("\n", "\\n")
        print(
            f"precondition-defer-recheck: {source} non-dict-and-non-list aggregate "
            f"(type={type(obj).__name__}); body prefix: {body_prefix!r}",
            file=sys.stderr,
        )
        sys.exit(1)  # guard-383: corrupt aggregate shape
    return obj


def _read_goals(source):
    """Read all active goals from world or agent queue.

    Uses the daemon via _rt (aspirations.py read CLI was deleted in the
    2026-05-14 cutover; _rt is the canonical Python -> daemon client).
    Parse path is g-115-766-tolerant via _tolerant_decode — see that
    helper for the contract. Applied via g-115-797-A3 (bravo audit
    catalog row 14) — replaces the prior silent-collapse
    `except json.JSONDecodeError: return []` that would hide corruption
    behind a "no goals to recheck" no-op and freeze precondition_unmet
    defers indefinitely (preconditions never re-evaluated).

    RtError handling — guard-383 fatal symmetry (rb-987):
    `all_goals = _read_goals("world") + _read_goals("agent")` at line 179
    is the canonical N>=2-source aggregator pattern. Per guard-383, a
    per-source error must be FATAL (sys.exit(1)) — a silent `return []`
    would write a complete-looking lie into the merged aggregate (e.g.
    agent-only queue presented as the entire portfolio). The exemplar
    consolidation-health.py corrected this in commit 28a3b7a (after the
    initial g-115-796 spec-prescribed silent return); sibling A4
    (parent-supersession-sweep.py) followed the corrected pattern. A3
    matches A4 / consolidation-health.py — NOT A1 (defer-recheck.py),
    which still carries the pre-correction silent-return pattern and is
    itself a candidate for an audit-followup fix.
    The single fail-open boundary is the caller's shell wrapper
    `|| echo WARN` (rb-347), never inside this aggregator.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[precondition-defer-recheck] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal — single fail-open boundary is wrapper
    data = _tolerant_decode(source, out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _age_hours(ts):
    if not ts:
        return None
    try:
        t = dt.datetime.fromisoformat(str(ts).replace("Z", ""))
        return (dt.datetime.now() - t).total_seconds() / 3600
    except Exception:
        return None


def _clear_defer(source, goal_id):
    """Clear both defer_reason and defer_reason_set_at on a goal.

    Two separate update-goal calls (the script doesn't support multi-field
    writes in one invocation; matches defer-recheck.py's clear semantics)."""
    rc1, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                     "--source", source, "update-goal",
                     goal_id, "defer_reason", "null"])
    rc2, _, _ = _py([str(SCRIPT_DIR / "aspirations.py"),
                     "--source", source, "update-goal",
                     goal_id, "defer_reason_set_at", "null"])
    return rc1 == 0 and rc2 == 0


# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=("Re-evaluate structured-precondition defers and clear "
                     "when all predicates pass. Avoids vacuous-truth bug via "
                     "explicit empty-list skip. Sibling: defer-recheck.py."),
    )
    ap.add_argument("--max-age-hours", type=float, default=2.0,
                    help="Minimum defer age before recheck (default 2h).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually clear defer_reason + defer_reason_set_at "
                         "(default: dry-run, reports would_clear list).")
    ap.add_argument("--output", choices=["json", "human"], default="json")
    ap.add_argument("--metrics-log", default=None,
                    help=("Path to JSONL metrics log. Default: "
                          "<WORLD_PATH>/precondition-defer-recheck-metrics.jsonl. "
                          "Pass empty string to disable."))
    args = ap.parse_args()
    metrics_path = _resolve_metrics_log(args.metrics_log)

    all_goals = _read_goals("world") + _read_goals("agent")

    scanned = 0
    eligible = 0
    evaluated = 0
    skipped_free_form = 0
    cleared = 0
    would_clear = []
    details = []

    for g in all_goals:
        scanned += 1
        reason = g.get("defer_reason") or ""

        # Eligibility filters (same shape as defer-recheck.py).
        if not reason.startswith("precondition_unmet:"):
            continue
        if g.get("status") not in ("pending", "in-progress"):
            continue
        if g.get("deferred_until"):
            # Structured time gate is the authoritative scheduler signal;
            # defer_reason is parallel narrative when deferred_until is set.
            continue
        age_h = _age_hours(g.get("defer_reason_set_at")
                           or g.get("started")
                           or g.get("created_at"))
        if age_h is None or age_h < args.max_age_hours:
            continue
        eligible += 1

        # Pre-filter: dict-structured predicates only (mirrors goal-selector.py
        # L967-981). String preconditions are LLM-judgment-only; free-form
        # narratives never reach this script's clear path.
        pcs_raw = (g.get("verification") or {}).get("preconditions") or []
        struct_pcs = [p for p in pcs_raw
                      if isinstance(p, dict) and "type" in p]

        if not struct_pcs:
            # Vacuous-truth guard: zero structured predicates ≠ "all pass".
            # The defer is free-form / string-only — LLM judgment territory.
            # Do NOT clear. Report skip with explicit reason.
            skipped_free_form += 1
            details.append({
                "goal_id": g["id"],
                "source": g["_source"],
                "age_hours": round(age_h, 1),
                "action": "skipped",
                "reason": ("no structured preconditions to evaluate — defer "
                           "is free-form, LLM judgment required"),
                "struct_pc_count": 0,
                "raw_pc_count": len(pcs_raw),
            })
            continue

        # In-process evaluate_all (NOT a subprocess — avoids exit-code-on-empty
        # vacuous-truth bug AND avoids Windows bash subprocess unreliability).
        # mode="all" returns one result per predicate; we want the full picture
        # for diagnostic reporting on partial-pass cases.
        evaluated += 1
        pc_results = evaluate_all(struct_pcs, mode="all", include_skippable=True)
        failed = [r for r in pc_results if not r.passed]

        if failed:
            details.append({
                "goal_id": g["id"],
                "source": g["_source"],
                "age_hours": round(age_h, 1),
                "action": "skipped",
                "reason": f"{len(failed)}/{len(pc_results)} structured predicates still failing",
                "struct_pc_count": len(struct_pcs),
                "failing_predicates": [
                    {"type": r.predicate_type,
                     "id": r.predicate_id,
                     "reason": r.reason}
                    for r in failed
                ],
            })
            continue

        # All structured predicates pass — clearable.
        entry = {
            "goal_id": g["id"],
            "source": g["_source"],
            "age_hours": round(age_h, 1),
            "action": "would_clear",
            "struct_pc_count": len(struct_pcs),
            "all_passed": True,
        }
        would_clear.append(g["id"])

        if args.apply:
            ok = _clear_defer(g["_source"], g["id"])
            entry["action"] = "cleared" if ok else "clear_failed"
            if ok:
                cleared += 1
                # LOAD-BEARING: log ONLY after the clear succeeds — same
                # invariant as defer-recheck.py (rb-468 metrics integrity).
                _append_metric(metrics_path, {
                    "type": "precondition_defer_cleared",
                    "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                    "goal_id": g["id"],
                    "source": g["_source"],
                    "aspiration_id": g.get("_aspiration_id"),
                    "age_hours_at_clear": round(age_h, 2),
                    "struct_pc_count": len(struct_pcs),
                    "clear_reason": "all_structured_predicates_passed",
                    "agent": os.environ.get("MIND_AGENT", "") or None,
                })
        details.append(entry)

    # Per-run summary — emitted regardless of clears, so consumers can compute
    # rate with a real denominator. Dry-runs log too (apply=false).
    _append_metric(metrics_path, {
        "type": "run_summary",
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "scanned": scanned,
        "eligible": eligible,
        "evaluated": evaluated,
        "skipped_free_form": skipped_free_form,
        "would_clear_count": len(would_clear),
        "cleared": cleared,
        "apply": args.apply,
        "max_age_hours": args.max_age_hours,
        "agent": os.environ.get("MIND_AGENT", "") or None,
    })

    result = {
        "scanned": scanned,
        "eligible": eligible,
        "evaluated": evaluated,
        "skipped_free_form": skipped_free_form,
        "cleared": cleared,
        "would_clear": would_clear,
        "details": details,
        "apply": args.apply,
        "metrics_log": str(metrics_path) if metrics_path else None,
    }

    if args.output == "human":
        print(f"scanned={scanned} eligible={eligible} evaluated={evaluated} "
              f"skipped_free_form={skipped_free_form} "
              f"would_clear={len(would_clear)} cleared={cleared}")
        for d in details:
            print(f"  {d['goal_id']}: {d['action']} ({d.get('reason','')})")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
