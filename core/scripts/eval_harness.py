#!/usr/bin/env python3
# domain-leak-exempt: framework evaluation infra — no domain strings; generic scorers only.
"""Eval harness — the framework's measurement-and-validation substrate (Phase 0 keystone).

IN-CODE INDEX of the evaluative substrate (full set + integration order in
docs/framework-research-2026-06-13/05-refactor-proposal.md):
  Phase 0  eval_harness             — held-out validation gate ("earn the keep")  [this file]
  Phase 1c mdl_gate                 — encode-time parsimony (anti-bloat dedup)
  Phase 1d retrieval_utility_report — learning-KPI flip (utilization over volume)
  Phase 2  complexity_budget        — subtractive-gradient instrument
  Phase 3  hypothesis_lineage       — Arbor hypothesis-tree edges + prune-on-refutation
  Phase 4  goal_budget              — boxed, continuously-scored execution
  Phase 5  collective_belief        — collective-level interpretability
These seven are deliberately flat, hermetic standalone scripts (the framework's convention),
not a package; each is independently testable and carries no import-time path resolution.

WHY THIS EXISTS
---------------
The framework is an excellent *generative* loop (it produces work, encodes lessons, forges
skills, adds gates) but it has historically lacked the *evaluative* counterpart: a way to ask
"did keeping this change actually make me better?" before retaining a self-modification. The
research pass (docs/framework-research-2026-06-13/) found this missing organ is the single
highest-leverage addition — it is what makes self-revision provably better-than-baseline
(SkillOpt), caps memory bloat at the source (MDL), and gives the system a trustworthy oversight
surface. The framework's motto "no terminal state" gains its missing half here: **"earn the keep."**

DESIGN (deliberately small — low cognitive load)
------------------------------------------------
This module is the *domain-free decision engine*. It does NOT know how to run a skill or score
a domain task — that is the caller's job (and is inherently deployment-specific). The clean
boundary: the caller scores a candidate against a corpus of cases (0.0..1.0 per case), and this
module owns the reusable, testable part — corpus management, a held-out train/holdout split,
weighted aggregation, and the **validation gate** that turns two score sets (before vs. after a
change) into a keep/reject verdict.

WHERE THE GATE APPLIES (and where it does NOT) — full-text boundary, SkillOpt §B
--------------------------------------------------------------------------------
The held-out gate is trustworthy only where a self-modification produces a *measurable* signal:
a skill edit with eval cases, a goal with executable verification criteria, a retrieval change
with scored queries. SkillOpt states the limit directly: the loop "is most directly applicable
when the target task has automatic verifiers, exact-match metrics, executable checks … For
open-ended domains where success is subjective, multi-dimensional, or costly to judge, the
validation gate may require stronger human or model-based evaluation." So in Ayoai-Mind this
gate cleanly governs the *scorable* self-mods; for subjective ones (a new prose rule, a tree-node
summary) it degrades to model/human judgment — it does not pretend to a number it cannot earn.
This is the honest boundary of "earn the keep," not a defect.

Three pieces, nothing more:
  1. Corpus model    — `EvalCase`, `load_cases()`.
  2. Scorers         — small library of generic output->[0,1] scorers the caller MAY use.
  3. Gate            — `aggregate()` + `gate()` -> `Verdict`. This is "earn the keep."

No import-time path resolution (hermetic: unit-testable without a bound agent). Invoke directly
(`python3 core/scripts/eval_harness.py ...`) like the other standalone analysis scripts; if the
dev team later wants it daemon-served, the logic is already isolated for a thin wrapper.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# --------------------------------------------------------------------------- #
# 1. Corpus model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalCase:
    """One held-out evaluation case.

    `weight` lets important cases count more in the aggregate. `holdout=True`
    marks a case reserved for the validation gate (the gate evaluates on the
    holdout split by default, so a self-modification cannot be "tuned to the
    test" on the same cases it is judged by). `baseline_score` is the reference
    score (e.g. the human-competent or pre-change level) for delta reporting.
    """

    id: str
    weight: float = 1.0
    holdout: bool = False
    baseline_score: Optional[float] = None
    tags: tuple = ()

    @staticmethod
    def from_dict(d: dict) -> "EvalCase":
        if "id" not in d:
            raise ValueError(f"eval case missing required 'id': {d!r}")
        w = float(d.get("weight", 1.0))
        if not math.isfinite(w) or w < 0:
            raise ValueError(f"eval case {d['id']!r} weight must be a finite "
                             f"non-negative number, got {w!r} (a negative weight "
                             "can invert the gate verdict)")
        return EvalCase(
            id=str(d["id"]),
            weight=w,
            holdout=bool(d.get("holdout", False)),
            baseline_score=(None if d.get("baseline_score") is None
                            else float(d["baseline_score"])),
            tags=tuple(d.get("tags", []) or ()),
        )


def load_cases(path) -> List[EvalCase]:
    """Load an eval corpus from a .jsonl (one case per line) or .json (list) file.

    Blank lines and `#`-prefixed comment lines in .jsonl are skipped. Raises a
    clear error rather than silently returning an empty corpus, because an empty
    corpus would make the gate pass everything vacuously (a dangerous silent
    failure for a validation gate).
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    rows: List[dict] = []
    if p.suffix == ".json":
        data = json.loads(text)
        rows = data if isinstance(data, list) else data.get("cases", [])
    else:  # .jsonl
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    cases = [EvalCase.from_dict(r) for r in rows]
    if not cases:
        raise ValueError(f"eval corpus {p} contains no cases — refusing (an empty "
                         "corpus makes the validation gate pass vacuously)")
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"eval corpus {p} has duplicate case ids: {dupes}")
    return cases


# --------------------------------------------------------------------------- #
# 2. Scorers — generic output -> [0.0, 1.0] helpers the caller may use.
#    (The caller is free to score however it likes; these cover common shapes.)
# --------------------------------------------------------------------------- #


def clamp01(x: float) -> float:
    """Clamp to [0,1]. The gate assumes scores live in this range.

    Rejects non-finite values (NaN/inf): NaN silently poisons the gate because
    `nan < 0` and `nan > 1` are both False, so a NaN would pass through here
    looking like a finite score and then make every gate comparison False —
    rejecting a real improvement or passing a real regression depending on
    policy. A non-finite score is a caller/scorer bug; fail loud.
    """
    if not math.isfinite(x):
        raise ValueError(f"score must be a finite number, got {x!r}")
    return 0.0 if x < 0 else 1.0 if x > 1 else float(x)


def exact_match(output, expected) -> float:
    """1.0 iff equal (string-compared, stripped), else 0.0."""
    return 1.0 if str(output).strip() == str(expected).strip() else 0.0


def numeric_closeness(output: float, target: float, tolerance: float) -> float:
    """1.0 at target, decaying linearly to 0.0 at `tolerance` away. tolerance>0."""
    if tolerance <= 0:
        raise ValueError("tolerance must be > 0")
    return clamp01(1.0 - abs(float(output) - float(target)) / tolerance)


def contains_all(output: str, required: Sequence[str]) -> float:
    """Fraction of `required` substrings present in `output` (case-insensitive)."""
    if not required:
        return 1.0
    hay = str(output).lower()
    hits = sum(1 for r in required if str(r).lower() in hay)
    return hits / len(required)


# --------------------------------------------------------------------------- #
# 3. Aggregation + the validation gate ("earn the keep")
# --------------------------------------------------------------------------- #

_SPLITS = ("all", "holdout", "train")
_POLICIES = ("strict_improve", "no_regression")


def _split_cases(cases: Sequence[EvalCase], split: str) -> List[EvalCase]:
    if split == "all":
        return list(cases)
    if split == "holdout":
        return [c for c in cases if c.holdout]
    if split == "train":
        return [c for c in cases if not c.holdout]
    raise ValueError(f"unknown split {split!r} (expected one of {_SPLITS})")


def aggregate(scores: Dict[str, float], cases: Sequence[EvalCase],
              split: str = "all") -> float:
    """Weighted-mean score over the chosen split.

    `scores` maps case-id -> score in [0,1]. Every case in the split MUST have a
    score (a missing score is a caller bug — fail loud rather than silently
    treating an unscored case as 0, which would understate regressions and
    quietly pass a gate it should fail).
    """
    selected = _split_cases(cases, split)
    if not selected:
        raise ValueError(f"no cases in split {split!r} — cannot aggregate")
    unknown = set(scores) - {c.id for c in cases}
    if unknown:
        # A score keyed to an id not in the corpus is almost always a typo that
        # silently drops the REAL case's score — exactly the kind of invisible
        # mis-scoring a validation gate must never tolerate. Fail loud.
        raise ValueError(f"scores reference unknown case ids (typo?): {sorted(unknown)}")
    missing = [c.id for c in selected if c.id not in scores]
    if missing:
        raise ValueError(f"scores missing for cases in split {split!r}: {missing}")
    total_w = sum(c.weight for c in selected)
    if total_w <= 0:
        raise ValueError("total case weight is non-positive")
    return sum(clamp01(scores[c.id]) * c.weight for c in selected) / total_w


@dataclass
class Verdict:
    """The gate's keep/reject decision for a self-modification."""
    passed: bool
    before: float
    after: float
    delta: float
    policy: str
    split: str
    n_cases: int
    reason: str

    def as_dict(self) -> dict:
        return {
            "passed": self.passed, "before": round(self.before, 6),
            "after": round(self.after, 6), "delta": round(self.delta, 6),
            "policy": self.policy, "split": self.split,
            "n_cases": self.n_cases, "reason": self.reason,
        }


def gate(before: Dict[str, float], after: Dict[str, float],
         cases: Sequence[EvalCase], policy: str = "no_regression",
         epsilon: float = 0.0, split: str = "holdout") -> Verdict:
    """Decide whether a self-modification has earned its keep.

    Compares the aggregate score BEFORE the change against AFTER, on the holdout
    split by default. Two policies:
      - "no_regression": keep iff after >= before - epsilon. The conservative
        default for changes whose value isn't a score bump (refactors, new gates)
        — they must simply not make things worse (epsilon here TOLERATES a
        noise-level dip so a change isn't rejected for sub-noise wiggle).
      - "strict_improve": keep iff after > before + epsilon. For changes claimed
        as improvements (a forged/edited skill, a new heuristic) — they must
        actually move the number, per SkillOpt's central finding that
        self-revision doesn't reliably beat baseline without this check.
    Note the asymmetry: epsilon is the same *noise band* in both, applied in each
    policy's lenient direction — it LOWERS the bar for no_regression (tolerate a
    small dip) and RAISES it for strict_improve (clear the noise upward). Same knob,
    opposite sign by policy; that is intentional, not a bug.

    epsilon is the noise-band / promotion margin, and it is the single knob that
    reconciles the two source papers (full-text review, Step 7):
      * epsilon = 0.0 with strict_improve == SkillOpt's gate exactly: accept only
        on a strictly-greater held-out score, "ties are rejected … the deployed
        skill never silently drifts" (2605.23904 §4.2).
      * epsilon ≈ 0.02 with strict_improve == Regimes' recommended setting: its
        default threshold of 0.0 "is too permissive at high baselines, where it
        admits within-noise promotions"; callers should require the delta to
        clear the measured noise band (2606.10241 §5.7/§8.2). Set epsilon from a
        real noise estimate when you have one.
    Small holdout sets: read a positive delta as *descriptive*, not significant
    (Regimes uses paired McNemar tests and warns against pooling correlated
    splits). n_cases is on the Verdict so the caller can apply that judgment.
    """
    if policy not in _POLICIES:
        raise ValueError(f"unknown policy {policy!r} (expected one of {_POLICIES})")
    if epsilon < 0:
        raise ValueError("epsilon must be >= 0")
    selected = _split_cases(cases, split)
    b = aggregate(before, cases, split)
    a = aggregate(after, cases, split)
    delta = a - b
    if policy == "no_regression":
        passed = a >= b - epsilon
        reason = (f"after {a:.4f} {'>=' if passed else '<'} before {b:.4f} "
                  f"- epsilon {epsilon} (no_regression)")
    else:  # strict_improve
        passed = a > b + epsilon
        reason = (f"after {a:.4f} {'>' if passed else '<='} before {b:.4f} "
                  f"+ epsilon {epsilon} (strict_improve)")
    return Verdict(passed=passed, before=b, after=a, delta=delta, policy=policy,
                   split=split, n_cases=len(selected), reason=reason)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _load_scores(path) -> Dict[str, float]:
    """Load a case-id -> score JSON object from a file.

    Validates each value is a finite real number — a JSON null (None) or bool is
    a caller bug that float() would otherwise crash on or silently coerce
    (True -> 1.0), so reject both with a clear error.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a JSON object {{case_id: score}}")
    out: Dict[str, float] = {}
    for k, v in data.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{path}: score for {k!r} must be a number, got {v!r}")
        if not math.isfinite(v):
            raise ValueError(f"{path}: score for {k!r} must be finite, got {v!r}")
        out[str(k)] = float(v)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Eval harness — held-out validation gate for self-modifications.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gate", help="decide whether a change earned its keep")
    g.add_argument("--cases", required=True, help="eval corpus (.jsonl or .json)")
    g.add_argument("--before", required=True, help="JSON {case_id: score} before the change")
    g.add_argument("--after", required=True, help="JSON {case_id: score} after the change")
    g.add_argument("--policy", default="no_regression", choices=_POLICIES)
    g.add_argument("--split", default="holdout", choices=_SPLITS)
    g.add_argument("--epsilon", type=float, default=0.0)

    s = sub.add_parser("score", help="aggregate one score set over a split")
    s.add_argument("--cases", required=True)
    s.add_argument("--scores", required=True)
    s.add_argument("--split", default="all", choices=_SPLITS)

    args = ap.parse_args(argv)
    cases = load_cases(args.cases)
    if args.cmd == "gate":
        v = gate(_load_scores(args.before), _load_scores(args.after), cases,
                 policy=args.policy, epsilon=args.epsilon, split=args.split)
        print(json.dumps(v.as_dict(), indent=2))
        return 0 if v.passed else 1
    if args.cmd == "score":
        print(json.dumps({"score": round(aggregate(_load_scores(args.scores), cases,
                                                    args.split), 6),
                          "split": args.split}, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
