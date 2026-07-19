#!/usr/bin/env python3
"""Pre-apply-consult DRIFT gate — decision helper (1).

Second, complementary layer to the per-goal advisory `pre-apply-consult-gate.py`
(g-115-826). That advisory fires at Phase 4 ONLY on cross-agent framework
Applies (handoff_from != agent) and is loud-but-non-blocking. This layer closes
two gaps it left open:
  1. Coverage: the advisory never fires on OWN-authored framework deep goals —
     which is exactly what the three consecutive misses on 2026-07-14
     (g-115-2194 / g-115-2195 / g-115-2179) were. This layer keys on
     work_class == framework (any authorship), not on handoff.
  2. Posture: `code-review-protocol.md` step 4 is honor-system and drifted to a
     100% miss rate on framework deep goals. "An advisory will not fix a 3/3
     miss rate — advisory is what it already is." This layer ENFORCES: on N
     consecutive framework-deep closes with retrieval performed=false it sets
     the `force_pre_apply_consult` WM sentinel, which aspirations-precheck
     consumes as an always-run gate on the NEXT iteration, so the LLM cannot
     reach goal selection without running retrieve.sh (or logging why it is not
     applicable). It does NOT hard-block the Edit tool (a fail-closed per-edit
     gate can wedge the loop, spec WORK item 3); the sentinel+precheck path is
     the proven, non-wedging shape used by four sibling gates
     (force_tree_maintain, fresh_eyes_dispatch_pending, force_metric_encoding_pending,
     pipeline_reconcile_pending).

This is the CONSUMER for the learning gate's already-logged, previously-ignored
`retrieval-summary: performed=false` signal (iteration-close.sh do_learning_gate).

PURE decision helper: NO working-memory I/O. iteration-close.sh owns the WM
read/write (bash `wm-read.sh` / `wm-set.sh`, mirroring the force_tree_maintain
template at iteration-close.sh:1545-1557); this helper only decides. That split
keeps the hot path thin and makes the decision unit-testable in isolation — the
ACCEPTANCE regression tests target `decide()` directly. Same shape as
`spark-fire-dedup.py` (pure args -> stdout; bash owns the state).

Streak semantics (interpretation B — count consecutive framework-deep closes'
consultation status; everything else is transparent):
  - framework-deep close that EDITED a framework file, performed=false
    -> increment (real drift continues)
  - framework-deep close that EDITED a framework file, performed=true
    -> reset to 0 (they consulted before the edit; good)
  - framework-CLASSIFIED deep close that edited NO framework file
    -> UNCHANGED (g-115-2655). A read-only framework diagnostic (tree scan,
    gate audit) is work_class=framework but touches no framework file, so there
    is nothing to pre-apply-consult FOR. Counting it as a "miss" let a run of
    diagnostics climb the streak on false pretenses and re-fire the gate every
    iteration — the false-positive class g-115-2655 fixes. The signal is the
    goal's own commit(s): iteration-close.sh derives framework_edited via
    `git log --grep=<goal-id>` (iteration-commit stamps the goal-id into every
    commit message) and passes it in.
  - routine OR non-framework close         -> UNCHANGED (not part of the
    population; a frequent routine goal must not silently reset a real
    framework-deep drift run, else the gate never fires on the observed
    interspersed pattern). This is the "must NOT become a tax on every close"
    ACCEPTANCE guarantee: a routine/non-framework close never trips AND never
    perturbs the streak.
Trip fires when a framework-file-editing deep miss pushes the streak to >=
threshold; it keeps firing while drift persists (each set is one-shot-consumed
by precheck), and a single framework-file-editing deep close that DID consult
clears it. framework_edited defaults to True when the signal is absent
(backward-compat + fail-safe: a git-signal failure must not silently disable
the gate's real-drift catch).

Fail-open: any error yields a no-op decision (streak reset to 0, no sentinel),
so a bug in this helper can never wedge the iteration close.

Output: JSON {new_streak, set_sentinel, work_class, is_framework, trips, reason}.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _resolve_work_class(category: str, explicit: str) -> str:
    """Return the goal's work_class. Prefer the explicit field; else resolve
    the category via the shared _work_class mapping. Fail-open to ''.

    An explicit "unclassified" is NOT a class — it is the stamper's fail-open
    default baked into the record at goal-creation time (aspirations.py
    resolves category->work_class through the same mapping, so an unmapped
    category gets "unclassified" stamped PERMANENTLY on the record). Treat it
    like the mapping's own "unclassified" and fall through to live category
    resolution, so a later mapping extension self-heals stale stamps.
    Incident (g-115-2438): g-115-2416 carried work_class="unclassified"
    (category framework-guardrails-and-gates was unmapped at creation), so its
    consulted deep close was TRANSPARENT to the streak — the performed=true
    reset never fired and the sentinel re-tripped on the next framework close."""
    if explicit and explicit != "unclassified":
        return explicit
    if not category:
        return ""
    try:
        from _work_class import resolve as _resolve  # type: ignore
        wc = _resolve(category)
        # "unclassified" is the mapping's fail-open default — treat as "no
        # tracked class" so an unmapped category never counts as framework.
        return "" if wc == "unclassified" else (wc or "")
    except Exception:
        return ""


def _read_goal_work_class(goal_id: str, source: str) -> str:
    """Self-read a goal's work_class from the aspirations store. Fail-open to
    '' — the caller (iteration-close.sh) uses this only when it did not already
    pass --work-class / --category, and a read miss must never trip the gate."""
    if not goal_id:
        return ""
    try:
        import _paths  # type: ignore
    except Exception:
        return ""
    base = None
    try:
        if source == "agent":
            # AGENT_DIR resolves to the bound agent's dir (MIND_AGENT).
            base = getattr(_paths, "AGENT_DIR", None)
        else:
            base = getattr(_paths, "WORLD_DIR", None)
    except Exception:
        base = None
    if base is None:
        return ""
    jsonl = Path(base) / "aspirations.jsonl"
    if not jsonl.is_file():
        return ""
    try:
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                asp = json.loads(line)
            except Exception:
                continue
            for g in asp.get("goals", []):
                if (g.get("id") or g.get("goal_id")) == goal_id:
                    return _resolve_work_class(
                        g.get("category") or "", g.get("work_class") or ""
                    )
    except Exception:
        return ""
    return ""


def decide(outcome: str, performed: bool, streak: int,
           work_class: str, threshold: int,
           framework_edited: bool = True) -> dict:
    """Pure decision. See module docstring 'Streak semantics'.

    framework_edited (g-115-2655): whether this goal ACTUALLY edited a framework
    file (core/.claude/CLAUDE.md/mind_api). A framework-CLASSIFIED deep close
    that edited none is a read-only diagnostic — transparent to the streak.
    Defaults True (backward-compat + fail-safe): when the signal is absent the
    gate behaves as before, preserving the real-drift catch.

    Returns {new_streak, set_sentinel, work_class, is_framework,
    framework_edited, trips, reason}.
    """
    is_framework = (work_class == "framework")
    is_deep = (str(outcome).strip().lower() == "deep")
    streak = max(0, int(streak))
    threshold = max(1, int(threshold))

    if is_deep and is_framework and framework_edited:
        if not performed:
            new_streak = streak + 1
            set_sentinel = new_streak >= threshold
            reason = (
                f"framework-deep close (edited a framework file) with retrieval "
                f"performed=false — streak {new_streak}/{threshold}"
                + (" (FORCE pre-apply consult next precheck)"
                   if set_sentinel else "")
            )
            trips = True
        else:
            new_streak = 0
            set_sentinel = False
            reason = ("framework-deep close (edited a framework file) DID "
                      "consult (performed=true) — streak reset")
            trips = False
    else:
        # Routine, non-framework deep, OR framework-classified deep that edited
        # no framework file (read-only diagnostic): transparent to the streak so
        # a frequent routine/diagnostic goal never resets a real framework-deep
        # drift run, and never trips (the "no tax on every close" guarantee).
        new_streak = streak
        set_sentinel = False
        if not is_deep:
            reason = "outcome != deep — streak unchanged (not counted)"
        elif not is_framework:
            reason = (f"work_class={work_class or 'none'} != framework — "
                      f"streak unchanged (not counted)")
        else:
            # is_framework and is_deep, but framework_edited is False.
            reason = ("framework-classified deep close edited no framework file "
                      "(read-only diagnostic) — streak unchanged (not counted, "
                      "g-115-2655)")
        trips = False

    return {
        "new_streak": new_streak,
        "set_sentinel": set_sentinel,
        "work_class": work_class,
        "is_framework": is_framework,
        "framework_edited": bool(framework_edited),
        "trips": trips,
        "reason": reason,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Pre-apply-consult drift gate decision helper (g-115-2201)")
    ap.add_argument("--outcome", required=True, help="deep|routine")
    ap.add_argument("--performed", required=True,
                    help="true|false — did retrieval fire for this goal")
    ap.add_argument("--streak", type=int, default=0,
                    help="current consecutive framework-deep-miss streak")
    ap.add_argument("--threshold", type=int, default=2,
                    help="misses before the sentinel is set (default 2)")
    # Framework determination — first non-empty wins:
    ap.add_argument("--work-class", default="",
                    help="explicit work_class (bypasses resolution; test hook)")
    ap.add_argument("--category", default="",
                    help="goal category to resolve via _work_class (test hook)")
    ap.add_argument("--goal", default="",
                    help="goal id — self-read work_class when --work-class/"
                         "--category absent")
    ap.add_argument("--source", default="world", help="world|agent")
    ap.add_argument("--framework-edited", default="true",
                    help="true|false — did this goal actually edit a framework "
                         "file (core/.claude/CLAUDE.md/mind_api). Default true "
                         "(backward-compat / fail-safe: preserve the real-drift "
                         "catch when the signal is unavailable). g-115-2655.")
    a = ap.parse_args()

    try:
        performed = str(a.performed).strip().lower() == "true"
        # Absent / anything-but-"false" -> True (fail-safe toward the gate's
        # drift-catching mission; only an explicit "false" makes it transparent).
        framework_edited = str(a.framework_edited).strip().lower() != "false"
        if a.work_class or a.category:
            wc = _resolve_work_class(a.category, a.work_class)
        else:
            wc = _read_goal_work_class(a.goal, a.source)
        out = decide(a.outcome, performed, a.streak, wc, a.threshold,
                     framework_edited)
    except Exception as e:  # fail-open: never wedge the iteration close
        out = {
            "new_streak": 0,
            "set_sentinel": False,
            "work_class": "",
            "is_framework": False,
            "framework_edited": True,
            "trips": False,
            "reason": f"error: {e} (fail-open no-op)",
        }
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
