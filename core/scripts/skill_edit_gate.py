#!/usr/bin/env python3
# domain-leak-exempt: framework eval gate prototype adopted into core — generic only, no domain strings.
"""Tier-1 skill-edit gate (earn-the-keep Phase 1, goal G2 / g-115-1465).

Adapts skill-evaluate.sh's 5-dimension quality judgment into the eval_harness
held-out gate. The LLM (or skill-evaluate's `score` subcommand) judges the OLD
and NEW skill on the 5 dims (good/average/poor); this maps them to [0,1] score
maps over an in-memory 5-case corpus and runs gate() -- "earn the keep" for a
skill edit, with NO corpus file and NO meta/eval/ dependency (so it is decoupled
from G1 and lands first).

Adopted from a staged phase-1 prototype: the staged
path shim is deleted now that this lives in core/scripts (eval_harness is a
plain sibling import). Wired into .claude/skills/forge-skill/SKILL.md Step ~3.5:
every forge scores candidate-vs-baseline and must clear strict_improve BEFORE
world/forged-skills.yaml registration. The `gate` CLI subcommand runs the gate,
logs the verdict to meta/gate-firings.jsonl via _gate_log (gate id
`eval-harness-forge-accept`, registered in core/config/gates.yaml), and on BLOCK
appends the rejected edit to meta/skill-rejected-edits.jsonl (negative memory).

CLI:
  python core/scripts/skill_edit_gate.py gate --new-judgments '{...}' [--old-judgments '{...}'] \
      [--policy strict_improve|no_regression] [--epsilon 0.0] [--skill-name X] [--caller Y]
    exit 0 = PASS (register), exit 1 = BLOCK (skip registration; buffered),
    exit 2 = malformed call (bad JSON / a judgment outside good|average|poor) --
             not a verdict, nothing logged; fix the call and re-run.
  python core/scripts/skill_edit_gate.py            -> runs the self-test (exit 0 on PASS).
"""
from __future__ import annotations
import argparse
import datetime
import json
import sys

import eval_harness as eh

GATE_ID = "eval-harness-forge-accept"
DIMS = ("safety", "completeness", "executability", "maintainability", "cost_awareness")
_LABEL = {"good": 1.0, "average": 0.5, "poor": 0.0}
# A new forge has no "before"; judge it against the human-competent reference.
BASELINE_JUDGMENT = {d: "average" for d in DIMS}  # all 0.5


def _dim_cases(holdout_dims=()):
    """One EvalCase per dimension. holdout_dims optionally reserves a split."""
    return [eh.EvalCase(id=d, weight=1.0, holdout=(d in holdout_dims)) for d in DIMS]


def _to_scores(judgments: dict) -> dict:
    """{dim: good|average|poor} -> {dim: float}. Missing/invalid dim = caller bug."""
    out = {}
    for d in DIMS:
        if d not in judgments:
            raise ValueError(f"missing judgment for dimension {d!r}")
        j = str(judgments[d]).strip().lower()
        if j not in _LABEL:
            raise ValueError(
                f"dimension {d!r}: judgment must be good|average|poor, got {judgments[d]!r}")
        out[d] = _LABEL[j]
    return out


def gate_skill_edit(old_judgments: dict, new_judgments: dict, *,
                    policy: str = "strict_improve", epsilon: float = 0.0,
                    split: str = "all", holdout_dims=()) -> eh.Verdict:
    """Gate a skill edit/forge: NEW must beat OLD on the 5-dim corpus. PURE -- no side effects.

    For a brand-NEW forge, pass old_judgments=BASELINE_JUDGMENT (all 'average'
    = the 0.5 human-competent reference); the new skill must clear it under
    strict_improve. split='all' because the 5 dims are orthogonal quality axes,
    not a train/test split -- true held-out validation is Tier-2's job.
    """
    cases = _dim_cases(holdout_dims)
    before = _to_scores(old_judgments)
    after = _to_scores(new_judgments)
    return eh.gate(before, after, cases, policy=policy, epsilon=epsilon, split=split)


def run_gate(old_judgments: dict, new_judgments: dict, *,
             policy: str = "strict_improve", epsilon: float = 0.0,
             caller: str = None, skill_name: str = None, holdout_dims=()) -> eh.Verdict:
    """gate_skill_edit + telemetry + negative-memory side effects. Returns the Verdict.

    Logs the verdict to meta/gate-firings.jsonl (gate id eval-harness-forge-accept)
    and, on BLOCK, appends the rejected edit to meta/skill-rejected-edits.jsonl.
    Telemetry/buffer imports are lazy so the pure gate + self-test stay
    dependency-light (they need only eval_harness).
    """
    verdict = gate_skill_edit(old_judgments, new_judgments, policy=policy,
                              epsilon=epsilon, holdout_dims=holdout_dims)
    decision = "pass" if verdict.passed else "block"
    try:
        import _gate_log
        _gate_log.log(GATE_ID, decision,
                      caller=caller or "skill_edit_gate.run_gate",
                      payload={"skill": skill_name, "policy": policy, "epsilon": epsilon},
                      extra=verdict.as_dict())
    except Exception:
        pass  # telemetry is best-effort; never break the gate
    if not verdict.passed:
        try:
            from _paths import META_DIR
            from _fileops import locked_append_jsonl
            locked_append_jsonl(META_DIR / "skill-rejected-edits.jsonl", {
                "ts": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "gate_id": GATE_ID,
                "skill": skill_name,
                "policy": policy,
                "epsilon": epsilon,
                "old_judgments": old_judgments,
                "new_judgments": new_judgments,
                "verdict": verdict.as_dict(),
            })
        except Exception:
            pass  # negative-memory buffer is best-effort; never break the gate
    return verdict


def _cli_gate(argv):
    p = argparse.ArgumentParser(
        prog="skill_edit_gate.py gate",
        description="Gate a skill forge/edit on the 5-dim eval_harness check. "
                    "exit 0 = PASS (register), exit 1 = BLOCK (skip + buffer), "
                    "exit 2 = malformed call (not a verdict; nothing logged).")
    p.add_argument("--new-judgments", required=True,
                   help="JSON {dim: good|average|poor} for the candidate skill.")
    p.add_argument("--old-judgments", default=None,
                   help="JSON {dim: good|average|poor}; omit for a new forge (uses the 0.5 baseline).")
    p.add_argument("--policy", default="strict_improve",
                   help="strict_improve (forges, default) | no_regression (refactors).")
    p.add_argument("--epsilon", type=float, default=0.0,
                   help="0.0 for strict_improve forges; ~0.02 for no_regression refactors.")
    p.add_argument("--skill-name", default=None, help="Skill name (for telemetry + buffer).")
    p.add_argument("--caller", default=None, help="Callsite label for telemetry.")
    args = p.parse_args(argv)
    # A malformed call is NOT a verdict. It used to surface as a traceback with
    # exit 1 -- the BLOCK code -- so a caller that abbreviated the vocabulary
    # ("g" for good, copied from a SKILL.md placeholder; measured 2026-08-30)
    # read its own typo as a quality rejection and skipped registration. Refuse
    # legibly on exit 2, before anything is logged or buffered.
    try:
        new_j = json.loads(args.new_judgments)
        old_j = json.loads(args.old_judgments) if args.old_judgments else BASELINE_JUDGMENT
        if not isinstance(new_j, dict) or not isinstance(old_j, dict):
            raise ValueError("judgments must be a JSON object {dimension: judgment}")
        verdict = run_gate(old_j, new_j, policy=args.policy, epsilon=args.epsilon,
                           caller=args.caller, skill_name=args.skill_name)
    except (ValueError, TypeError) as exc:  # JSONDecodeError is a ValueError
        print(f"skill_edit_gate: malformed call, not a verdict -- {exc}. "
              f"Judgments are the full words good|average|poor (not g|a|p) for each of "
              f"{', '.join(DIMS)}; nothing was logged or buffered. Fix the call and re-run.",
              file=sys.stderr)
        sys.exit(2)
    print(json.dumps(verdict.as_dict()))
    sys.exit(0 if verdict.passed else 1)


def _self_test():
    ok = True
    # 1. edit improves completeness average->good, others equal -> strict PASS
    old = {"safety": "good", "completeness": "average", "executability": "good",
           "maintainability": "average", "cost_awareness": "average"}
    new = dict(old, completeness="good")
    v = gate_skill_edit(old, new)
    print("improve completeness (strict eps=0):", v.as_dict())
    ok &= v.passed
    # 2. edit regresses safety good->poor -> strict FAIL and no_regression FAIL
    bad = dict(old, safety="poor")
    print("regress safety (strict):", gate_skill_edit(old, bad).passed, "(expect False)")
    print("regress safety (no_reg eps=0.02):",
          gate_skill_edit(old, bad, policy="no_regression", epsilon=0.02).passed, "(expect False)")
    ok &= not gate_skill_edit(old, bad).passed
    ok &= not gate_skill_edit(old, bad, policy="no_regression", epsilon=0.02).passed
    # 3. new forge: all-good clears the average baseline under strict
    great = {d: "good" for d in DIMS}
    v3 = gate_skill_edit(BASELINE_JUDGMENT, great)
    print("new forge all-good vs baseline (strict):", v3.passed, "(expect True)")
    ok &= v3.passed
    # 4. new forge: all-average ties the baseline -> strict FAIL (must beat, not tie)
    v4 = gate_skill_edit(BASELINE_JUDGMENT, BASELINE_JUDGMENT)
    print("new forge all-average vs baseline (strict):", v4.passed, "(expect False -- ties rejected)")
    ok &= not v4.passed
    print("SELF-TEST:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        _cli_gate(sys.argv[2:])
    else:
        _self_test()
