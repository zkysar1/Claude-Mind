#!/usr/bin/env python3
"""Trivial-Goal Classifier — lightweight goal mode (; design ).

PRE-execution classifier: predicts whether a goal is TRIVIAL (no learning worth
extracting) so Phase 4 can skip the ~17 ceremony sub-phases in the Phase 3.9-4.5
window (intelligent retrieval, mid-exec drift/chunk encoding, episode chaining,
causal-enabler scan, experience archival, knowledge reconciliation). Returns
"trivial" ONLY when ALL THREE booleans hold (conservative AND-gate); ANY
uncertainty OR error -> "full".

COMPLEMENTARY, not duplicative (see brief s2): the POST-execution `routine`
outcome_class already skips Phase 4.25 + 6, and recurring-close.sh already
collapses Phases 5/8/9.5/10 for recurring goals. This classifier is the only
mechanism that reaches the PRE/mid/immediate-post band, because it fires before
execution rather than after.

ASYMMETRIC RISK (the decisive design constraint): a false-negative (a trivial
goal treated as full) costs only overhead; a false-positive (a real goal treated
as trivial) skips learning and is recoverable ONLY via the Phase-4-post escape
hatch. So every ambiguity resolves to "full", and the master flag defaults OFF
(g-306-08: ship a behavior-changing factor flag-gated, validate before
activating).

THE THREE BOOLEANS (all must hold for "trivial"):
  1. empty_verification_checks — goal.verification is null OR verification.checks
     is absent/empty. (Nothing to verify deeply.)
  2. no_diff_expected — explicit goal.produces_diff is False, OR goal.skill
     matches a configured pure-action skill substring, OR goal.category is in
     the configured action_only_categories allowlist. NEVER inferred from a
     code/framework/analysis category (those are simply absent from the
     allowlist; never_trivial_categories double-guards as defense-in-depth).
  3. no_retrieval_call — explicit goal.retrieval == "none", OR goal.skill is a
     configured pure-action skill. A goal whose success depends on judgment
     fails this.

FAIL-TO-FULL CONTRACT (the hot-path difference from the rb-428 detective
family): this classifier runs INSIDE Phase-4 execution, so — unlike
defer-drift-check.py / precondition-defer-recheck.py which sys.exit(1) on a
read error (guard-383 fatal-source contract) — it MUST degrade to "full" on
ANY failure (daemon down, goal not found, parse failure, config error, or even
a SystemExit raised by a decode helper). It never blocks execution and never
exits non-zero.

The pure classify_goal(goal, cfg) function is unit-testable with synthetic
goals; only _find_goal()'s daemon read is impure (same split as
defer-drift-check.py's _classify_drift vs main).

Invoked via `py -3 core/scripts/trivial-goal-classify.py <goal-id> --source
<world|agent>`. It is NOT a daemon .sh wrapper + endpoint (it is a read-only
local classifier with no shared-state mutation, so the daemon-only architecture
rule does not apply); it DOES use the _rt daemon client for the goal READ, like
defer-drift-check.py.

Design brief: agents/zeta/temp/g-305-02-lightweight-mode-brief.md.
"""

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_ROOT.parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Domain-AGNOSTIC defaults. Empty allowlists mean nothing auto-classifies
# trivial via inference — only explicit goal fields (produces_diff / retrieval)
# can. The domain extends the allowlists (mirrors class_balance.targets,
# "domain supplies this"). Master flag OFF by default ().
DEFAULT_CONFIG = {
    "enabled": False,
    "pure_action_skills": [],
    "action_only_categories": [],
    "never_trivial_categories": [],
}


def load_config():
    """Load the lightweight_mode block from aspirations.yaml via the config
    overlay (so meta/config-overrides.yaml + world/config/aspirations.yaml
    overrides apply, matching goal-selector.load_recurring_config). Fail-soft to
    DEFAULT_CONFIG on ANY error — a config read failure must degrade to disabled
    (classifier then returns "full"), never raise into the hot path."""
    cfg = dict(DEFAULT_CONFIG)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_config_overlay", SCRIPT_DIR / "_config_overlay.py")
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        asp = overlay.merged_config("aspirations.yaml")
        lm = asp.get("lightweight_mode", {}) if isinstance(asp, dict) else {}
        if isinstance(lm, dict):
            if isinstance(lm.get("enabled"), bool):
                cfg["enabled"] = lm["enabled"]
            for k in ("pure_action_skills", "action_only_categories",
                      "never_trivial_categories"):
                v = lm.get(k)
                if isinstance(v, list):
                    cfg[k] = [str(x).lower() for x in v]
    except Exception:
        pass
    return cfg


def _skill_is_pure_action(skill, cfg):
    """True iff goal.skill matches a configured pure-action skill (substring,
    case-insensitive). Empty/null skill -> False (cannot infer pure-action from
    absence)."""
    if not skill:
        return False
    s = str(skill).lower()
    return any(tok in s for tok in cfg.get("pure_action_skills", []))


def empty_verification_checks(goal):
    """Boolean 1: nothing to verify deeply. verification null/non-dict -> True;
    checks absent/empty -> True; checks non-empty -> False."""
    v = goal.get("verification")
    if not isinstance(v, dict):
        return True
    return not v.get("checks")  # None or [] -> True


def no_diff_expected(goal, cfg):
    """Boolean 2: the goal is predicted to change no files. Explicit
    produces_diff wins; else pure-action skill or action-only category. NEVER
    inferred from a code/framework/analysis category (those are absent from the
    allowlist)."""
    pd = goal.get("produces_diff")
    if pd is False:
        return True
    if pd is True:
        return False  # explicit: this goal DOES produce a diff
    if _skill_is_pure_action(goal.get("skill"), cfg):
        return True
    cat = str(goal.get("category") or "").lower()
    return cat in cfg.get("action_only_categories", [])


def no_retrieval_call(goal, cfg):
    """Boolean 3: the goal needs no knowledge retrieval. Explicit retrieval:none
    wins; an explicit non-"none" retrieval mode means it DOES need retrieval;
    else fall back to pure-action skill inference."""
    r = goal.get("retrieval")
    if isinstance(r, str):
        return r.lower() == "none"
    return _skill_is_pure_action(goal.get("skill"), cfg)


def classify_goal(goal, cfg):
    """PURE: classify ONE goal dict. Returns (verdict, reasons). verdict is
    "trivial" iff all three booleans hold AND the category is not in the
    never_trivial denylist; else "full". Unit-testable without the daemon."""
    cat = str(goal.get("category") or "").lower()
    if cat in cfg.get("never_trivial_categories", []):
        return "full", {"never_trivial_category": cat}
    b1 = empty_verification_checks(goal)
    b2 = no_diff_expected(goal, cfg)
    b3 = no_retrieval_call(goal, cfg)
    reasons = {
        "empty_verification_checks": b1,
        "no_diff_expected": b2,
        "no_retrieval_call": b3,
    }
    verdict = "trivial" if (b1 and b2 and b3) else "full"
    return verdict, reasons


def _find_goal(goal_id, source):
    """Impure: read the goal by id from the world|agent queue via the daemon
    client (_rt), mirroring defer-drift-check.py. Returns the goal dict or None.
    Any error (RtError, decode failure, or a SystemExit raised by the decode
    helper) propagates to main()'s fail-to-full handler — this function does NOT
    sys.exit (that would violate the hot-path contract)."""
    import _rt
    out = _rt.aspirations_read(source=source, active=True)
    data = _rt.tolerant_decode_aggregate(f"trivial-goal-classify: {source}", out)
    if data is None:
        return None
    asps = data.get("aspirations") if isinstance(data, dict) else data
    for asp in asps or []:
        for g in asp.get("goals", []) or []:
            if g.get("id") == goal_id:
                return g
    return None


def main():
    ap = argparse.ArgumentParser(
        description=("Predict whether a goal is trivial (skip Phase 3.9-4.5 "
                     "ceremony). Conservative AND-gate; fail-to-full; master "
                     "flag default-OFF. Design: g-305-02."))
    ap.add_argument("goal_id", help="Goal id to classify (e.g. g-305-15)")
    ap.add_argument("--source", choices=["world", "agent"], default="world")
    ap.add_argument("--output", choices=["plain", "json"], default="plain",
                    help="plain prints just trivial|full; json adds reasons")
    ap.add_argument("--assume-enabled", action="store_true",
                    help=("bypass the master-flag gate (TEST/diagnostic only — "
                          "exercises classification logic when enabled=false)"))
    args = ap.parse_args()

    verdict = "full"
    reasons = {}
    # FAIL-TO-FULL: catch Exception AND SystemExit — _rt.tolerant_decode_aggregate
    # may sys.exit(1) on a malformed daemon body (guard-383 contract in the
    # detective family); in the hot path that must degrade to "full", not abort.
    # (argparse's own sys.exit on --help / bad args runs BEFORE this try, so it
    # is unaffected.) KeyboardInterrupt is NOT caught (it is a separate
    # BaseException), so a manual interrupt still works.
    try:
        cfg = load_config()
        if not cfg["enabled"] and not args.assume_enabled:
            reasons = {"disabled": True}
        else:
            goal = _find_goal(args.goal_id, args.source)
            if goal is None:
                reasons = {"goal_not_found": args.goal_id}
            else:
                verdict, reasons = classify_goal(goal, cfg)
    except (Exception, SystemExit) as e:
        verdict = "full"
        reasons = {"error": str(e)[:200]}

    if args.output == "json":
        print(json.dumps({"goal_id": args.goal_id, "source": args.source,
                          "verdict": verdict, "reasons": reasons}))
    else:
        print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
