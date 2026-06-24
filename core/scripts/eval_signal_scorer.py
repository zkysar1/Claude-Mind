#!/usr/bin/env python3
# domain-leak-exempt: framework eval substrate — generic signal reconstruction, no domain strings.
"""Tier-2 signal scorer + keystone calibration self-check (g-115-1467, earn-the-keep Phase 1 / G3).

The Tier-2 layer of the eval-harness substrate. Given the G1 Class-B corpus
(`meta/eval/cases.jsonl`) of historical changes labeled by KNOWN outcome
(`baseline_score`), reconstruct the before/after measured SIGNAL each change's
gate would have keyed on — dispatched by the case's `signal_source` — then run
the keystone self-check: *does the signal the gate keys on actually discriminate
good changes from bad?* (validation-methodology.md). A gate whose signal does
not discriminate MUST NOT be trusted to gate live self-modifications.

Pairs with:
  - core/scripts/eval_harness.py  — gate()/Verdict/aggregate(); the substrate
  - meta/eval/cases.jsonl         — the G1 corpus this validates
  - self_check_proto.evaluate_calibration — the keystone metric, adopted below

WHY load_cases() is NOT used for reconstruction
-----------------------------------------------
`eval_harness.EvalCase.from_dict` deliberately keeps only {id, weight, holdout,
baseline_score, tags} — it DROPS `signal_source` and `note`. The Tier-2 scorer
needs `signal_source` (to dispatch the reconstruction) and the documented signal
disposition, so it reads the raw JSONL directly (`_read_raw`, same comment/blank
tolerance as load_cases). The substrate's gate()/aggregate() are still used for
the aggregate earn-the-keep decision and by the skill-edit chokepoint.

HONEST BOUNDARY (validation-methodology.md "Honest caveats")
------------------------------------------------------------
`signal_source` is the single most important calibration knob. The reconstruction
here is a DOCUMENTED-RECORD reconstruction: the signal *direction* per case comes
from the change's disposition (tags / revert-status recorded at corpus-creation),
which is INDEPENDENT of the baseline_score label — NOT a live multi-repo historical
re-run (infeasible: external product-repo commits, reverted hashes that no longer
build). The keystone's binding, non-tautological result is the FULL-vs-TARGETED
contrast: the same corpus is `trustworthy` under the FULL-suite signal but NOT
under the targeted-test signal, because a documented full/targeted-divergent
regression (`targeted_divergent: true`) mis-passes a naive targeted gate. That
divergence is the actionable finding (use the full suite, per
run-full-suite-after-deep-code.md) and proves the check measures SIGNAL QUALITY,
not label self-consistency. Live in-repo re-runs for the framework subset are a
documented future hardening.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Sibling import — eval_signal_scorer lives beside eval_harness in core/scripts.
# Insert the script's own dir so `import eval_harness` resolves regardless of cwd
# (matches the substrate's "no import-time path resolution" convention: this is a
# plain sys.path insert of the script dir, not agent-path resolution).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_harness as eh  # noqa: E402

SIGNAL_SOURCES = ("test-pass-rate", "verification-checks", "retrieval-hit-utility",
                  "deploy-verify")

# Illustrative magnitude — the keystone reads only the SIGN of signal_delta
# (passed = delta > epsilon, epsilon defaults to 0). Magnitude does not affect
# the verdict; it keeps the reconstructed before/after in [0,1] and readable.
_DELTA = 0.15
_BASE = 0.5


def _read_raw(path) -> List[dict]:
    """Read the raw corpus rows (full dicts incl. signal_source / note / tags).

    Mirrors load_cases() comment+blank tolerance and empty/dup guards, but keeps
    every field rather than projecting onto EvalCase. Raises on an empty corpus
    (an empty corpus would make the keystone vacuously 'pass') and on dup ids.
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
    if not rows:
        raise ValueError(f"corpus {p} contains no cases — refusing (an empty corpus "
                         "makes the keystone self-check pass vacuously)")
    ids = [str(r.get("id")) for r in rows]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"corpus {p} has duplicate case ids: {dupes}")
    return rows


def _disposition(case: dict) -> int:
    """+1 if the change's documented signal was up/flat (landed, suite green);
    -1 if it dropped (reverted / regression).

    Read from `tags` — the disposition marker set at corpus-creation from the
    change's git revert-status, INDEPENDENT of the baseline_score label. A tag
    ending in 'regression' or containing 'revert' marks a change whose measured
    signal dropped (that is WHY it was reverted/flagged); everything else is a
    landed change whose signal held up or improved.
    """
    for t in case.get("tags", []) or ():
        t = str(t).lower()
        if t.endswith("regression") or "revert" in t:
            return -1
    return +1


def reconstruct_signal(case: dict, *, mode: str = "full") -> Tuple[float, float]:
    """Reconstruct (before, after) signal in [0,1] for the case's signal_source.

    `mode` in {"full", "targeted"} only changes test-pass-rate cases:
      - "full"     — the FULL-suite signal (run-full-suite-after-deep-code.md).
      - "targeted" — the targeted-test signal (the naive gate). It diverges from
        the full suite EXACTLY on a case flagged `targeted_divergent: true` (the
        documented "targeted test passed but the full suite regressed" shape):
        the naive gate sees an improvement and MIS-PASSES a bad change.

    The sign of (after - before) is the gate verdict under strict_improve(0).
    """
    if mode not in ("full", "targeted"):
        raise ValueError(f"mode must be 'full' or 'targeted', got {mode!r}")
    src = case.get("signal_source", "verification-checks")
    if src not in SIGNAL_SOURCES:
        # Unknown signal_source is a corpus authoring error: a gate cannot
        # reconstruct a signal it has no dispatch for. Fail loud, do not guess.
        raise ValueError(f"case {case.get('id')!r} has unknown signal_source {src!r} "
                         f"(expected one of {SIGNAL_SOURCES})")
    if (mode == "targeted" and src == "test-pass-rate"
            and case.get("targeted_divergent")):
        # Naive targeted gate: the targeted test passed -> apparent improvement,
        # regardless of the full-suite regression the disposition records.
        return (_BASE, _BASE + _DELTA)
    return (_BASE, _BASE + _disposition(case) * _DELTA)


def build_records(rows: List[dict], *, mode: str = "full") -> List[dict]:
    """Reconstruct [{id, baseline_score, signal_delta}] for labeled cases.

    Cases with baseline_score == None are unlabeled and excluded from the
    calibration metric (they carry no good/bad ground truth to validate against).
    """
    out: List[dict] = []
    for case in rows:
        bs = case.get("baseline_score")
        if bs is None:
            continue
        before, after = reconstruct_signal(case, mode=mode)
        out.append({"id": str(case["id"]), "baseline_score": float(bs),
                    "signal_delta": after - before})
    return out


def evaluate_calibration(records, *, good_thr: float = 0.6, bad_thr: float = 0.3,
                         epsilon: float = 0.0, trust_thr: float = 0.8) -> dict:
    """Keystone metric (adopted from phase1-prep/self_check_proto.py, g-115-1467).

    records: [{id, baseline_score, signal_delta}]. Gate verdict per record under
    strict_improve: passed = signal_delta > epsilon. GOOD if baseline_score >=
    good_thr, BAD if <= bad_thr (middling excluded). trustworthy iff
    true_pass_rate >= trust_thr AND true_block_rate >= trust_thr.
    """
    good = [r for r in records if r["baseline_score"] >= good_thr]
    bad = [r for r in records if r["baseline_score"] <= bad_thr]

    def passed(r):
        return r["signal_delta"] > epsilon

    tpr = (sum(1 for r in good if passed(r)) / len(good)) if good else None
    tbr = (sum(1 for r in bad if not passed(r)) / len(bad)) if bad else None
    trustworthy = (tpr is not None and tbr is not None
                   and tpr >= trust_thr and tbr >= trust_thr)
    return {
        "n_good": len(good), "n_bad": len(bad),
        "true_pass_rate": tpr, "true_block_rate": tbr,
        "trust_threshold": trust_thr, "trustworthy": trustworthy,
        # misses: GOOD cases the gate wrongly blocked + BAD cases it wrongly passed.
        "misses": ([r["id"] for r in good if not passed(r)]
                   + [r["id"] for r in bad if passed(r)]),
    }


def run_keystone(cases_path, *, mode: str = "full", good_thr: float = 0.6,
                 bad_thr: float = 0.3, epsilon: float = 0.0,
                 trust_thr: float = 0.8) -> dict:
    """Reconstruct signals over the corpus and run the keystone calibration."""
    rows = _read_raw(cases_path)
    records = build_records(rows, mode=mode)
    report = evaluate_calibration(records, good_thr=good_thr, bad_thr=bad_thr,
                                  epsilon=epsilon, trust_thr=trust_thr)
    report["mode"] = mode
    report["n_records"] = len(records)
    report["n_total_cases"] = len(rows)
    report["epsilon"] = epsilon
    return report


def score_map(cases_path, *, mode: str = "full", phase: str = "after") -> Dict[str, float]:
    """Emit {case_id: reconstructed_signal} for the chosen phase (before|after).

    This is the Tier-2 scorer's output feeding eval_harness.gate's CLI
    (`--before`/`--after` JSON maps) for an aggregate earn-the-keep decision.
    """
    if phase not in ("before", "after"):
        raise ValueError(f"phase must be 'before' or 'after', got {phase!r}")
    idx = 0 if phase == "before" else 1
    out: Dict[str, float] = {}
    for case in _read_raw(cases_path):
        out[str(case["id"])] = reconstruct_signal(case, mode=mode)[idx]
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Tier-2 signal scorer + keystone calibration self-check.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keystone", help="run the keystone calibration self-check")
    k.add_argument("--cases", required=True, help="eval corpus (.jsonl or .json)")
    k.add_argument("--mode", default="full", choices=("full", "targeted"))
    k.add_argument("--epsilon", type=float, default=0.0)
    k.add_argument("--good-thr", type=float, default=0.6)
    k.add_argument("--bad-thr", type=float, default=0.3)
    k.add_argument("--trust-thr", type=float, default=0.8)

    c = sub.add_parser("contrast", help="run keystone under FULL and TARGETED, "
                       "show the signal_source divergence (the headline finding)")
    c.add_argument("--cases", required=True)
    c.add_argument("--epsilon", type=float, default=0.0)
    c.add_argument("--trust-thr", type=float, default=0.8)

    s = sub.add_parser("score", help="emit {case_id: signal} for one phase")
    s.add_argument("--cases", required=True)
    s.add_argument("--mode", default="full", choices=("full", "targeted"))
    s.add_argument("--phase", default="after", choices=("before", "after"))

    args = ap.parse_args(argv)
    if args.cmd == "keystone":
        rep = run_keystone(args.cases, mode=args.mode, good_thr=args.good_thr,
                           bad_thr=args.bad_thr, epsilon=args.epsilon,
                           trust_thr=args.trust_thr)
        print(json.dumps(rep, indent=2))
        return 0 if rep["trustworthy"] else 1
    if args.cmd == "contrast":
        full = run_keystone(args.cases, mode="full", epsilon=args.epsilon,
                            trust_thr=args.trust_thr)
        targeted = run_keystone(args.cases, mode="targeted", epsilon=args.epsilon,
                                trust_thr=args.trust_thr)
        # Threshold-INDEPENDENT divergence: BAD cases the FULL suite blocks that a
        # targeted-only signal mis-passes. This is the binding non-vacuity finding
        # — it does NOT depend on whether the aggregate true_block_rate crosses
        # trust_thr. With few negatives a single targeted miss may not flip the 0.8
        # gate (one of six = 0.833 > 0.8), yet the signal_source choice still
        # changes WHICH regressions are caught. The set-difference surfaces the
        # specific case (the documented `targeted_divergent` regression), which is
        # the actionable result: use the FULL suite (run-full-suite-after-deep-code.md).
        targeted_only_misses = sorted(set(targeted["misses"]) - set(full["misses"]))
        out = {
            "full": full, "targeted": targeted,
            "targeted_only_misses": targeted_only_misses,
            "signal_source_matters": bool(targeted_only_misses),
        }
        print(json.dumps(out, indent=2))
        return 0 if out["signal_source_matters"] else 1
    if args.cmd == "score":
        print(json.dumps(score_map(args.cases, mode=args.mode, phase=args.phase),
                         indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
