#!/usr/bin/env python3
"""Scorer-Criterion CONTRIBUTION Probe — does a goal-selector criterion move the RANKING?

Given a criterion name, reports four things about the CURRENT candidate pool:

  1. FIRES              — does the term contribute a nonzero addend to any candidate?
  2. MAGNITUDE          — the distribution of its weighted addend across the pool
  3. RANK DELTA         — how the ranking changes with vs without the term
  4. NOISE-FREE RANK    — the deterministic ranking with exploration_noise removed

Answers a question NEITHER neighbour answers, and the distinction is the whole
point of this script (gap-033):

  "is the term WIRED?"            -> a grep over goal-selector.py
  "is its INPUT FIELD populated?" -> scoring-criterion-audit.py (field coverage:
                                     dead_field / degenerate_field / source_skew)
  "does it move the RANKING?"     -> THIS script

A criterion can be correctly wired AND correctly firing AND have a fully
populated input field, and still change no outcome — because what it competes
against lies outside the pool it was calibrated for. That case is invisible to
both neighbours and is exactly what cost two agents ~6-10 tool calls each plus a
refuted hypothesis (foxtrot g-115-3448 / rb-5341; zeta g-115-5817).

--------------------------------------------------------------------------
WHY ONE SELECTOR RUN, AND WHY THAT IS LOAD-BEARING (guard-3562)
--------------------------------------------------------------------------
`exploration_noise` is re-sampled on every scoring pass, so RE-RUNNING the
selector RE-ROLLS the ranking. Any answer computed by diffing two runs is
therefore invalid by construction — it cannot separate the term's effect from
the noise lottery. Measured (zeta, 2026-08-14, g-115-5817): five runs on an
UNCHANGED queue produced five contradictory observed ranks (899 / 1034 / 1044 /
1084 / 1085) against one stable noise-free rank (1068 of 1146).

This probe takes exactly ONE run and does all four computations by SUBTRACTION
on that single snapshot. `breakdown` values are already the WEIGHTED addends
(goal-selector.py: `{k: raw[k] * WEIGHTS[k]}`) and the noise term is purely
additive (`noise_weight = epsilon * noise_scale`; `total += raw[...] *
noise_weight`), so:

    noise-free score   = score - breakdown["exploration_noise"]
    score without C    = score - breakdown[C]

Subtracting REMOVES the noise rather than re-sampling it, which is what makes
the noise-free rank reproducible across invocations — the property the caller
is told to verify (`--self-test` asserts it directly).

RANK DELTA IS COMPUTED ON THE NOISE-FREE BASIS, not the observed one. A delta
taken against the observed ranking is contaminated by the very lottery this
script exists to remove, and at high epsilon that contamination dominates the
signal being measured.

--------------------------------------------------------------------------
THE NOISE BAND IS PER-AGENT — NEVER A CONSTANT
--------------------------------------------------------------------------
The ceiling on the noise addend is `epsilon * noise_scale`, and epsilon is
per-agent mutable state (`<agent>/developmental-stage.yaml -> exploration.epsilon`).
This script READS it from the selector's own `exploration_params` block rather
than hardcoding it, so the band it reports is always this agent's. A high-epsilon
agent sees a band that can reorder the ranking outright; a low-epsilon agent sees
only two-decimal tie-breaking. Reporting a constant would be wrong for every
agent but one. See rb-7809 (per-agent scope), rb-5482 (the pick is a noise
lottery at epsilon >= 0.4), guard-1895, guard-3562.

--------------------------------------------------------------------------
RECOMMENDER, NOT JUDGE
--------------------------------------------------------------------------
Reads only. Never edits goal-selector.py, weights, or any goal. Output is JSON /
human table on stdout; persistence is the caller's job. Deliberately does NOT
write its own log — concurrent runs would race on a multi-KB single-line append
(POSIX atomicity holds only below PIPE_BUF=4096; Windows offers none). Same
posture as its sibling `scoring-criterion-audit.py`.

Usage:
  py -3 core/scripts/scorer-criterion-contribution-probe.py --criterion <name>
        [--output json|human]     (default: human)
        [--top K]                 (rank-delta detail rows, default 10)
        [--list]                  (list probeable criteria from a live run)
        [--self-test]             (offline invariant checks; no selector run)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

# guard-580: never build a subprocess argv with a bare "bash" argv[0]. On win32
# CreateProcess searches System32 BEFORE PATH, so "bash" resolves to the WSL
# launcher and blocks FOREVER on a dead LxssManager — the parent hangs in
# communicate() with a 0-CPU child. bash_cmd() resolves the real interpreter and
# passes the script path as posix (guard-581: bash silently strips the
# backslashes of a str(WindowsPath)). The script's own dir is on sys.path
# because this file is invoked as a script, so the sibling import resolves.
from _runtime_bash import bash_cmd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SELECTOR = "core/scripts/goal-selector.sh"
NOISE_KEY = "exploration_noise"

# Criteria whose breakdown key folds MULTIPLE addends into one number. For these
# the rank-delta is the COMPOSITE's total effect and can never be attributed to a
# single addend — the probe must say so rather than emit a confidently misleading
# number. guard-2412: `raw["directive_boost"] > 0` cannot tell a standing user
# directive from a board directive, because strategic_focus_boost is summed in.
COMPOSITE_CRITERIA = {
    "directive_boost": (
        "folds strategic_focus_boost + directive_boost_score into one key "
        "(guard-2412) — the delta below is the COMPOSITE's total effect and "
        "cannot be attributed to either addend"
    ),
}


def run_selector(timeout=300):
    """ONE selector run. Returns (candidates, error). Never retries — a retry
    would re-roll the noise and silently invalidate every number below."""
    try:
        proc = subprocess.run(
            bash_cmd(SELECTOR),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"goal-selector timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"goal-selector invocation failed: {exc}"

    out = proc.stdout.strip()
    if not out:
        # An empty stdout is NOT an empty pool — it is a failed run. Reporting
        # "0 candidates" here would manufacture exactly the confident zero this
        # whole script family exists to prevent (guard-2298: print the SHAPE and
        # the BYTE COUNT beside any count).
        return None, (
            f"goal-selector produced NO stdout (rc={proc.returncode}, "
            f"stderr={proc.stderr.strip()[:300]!r}). This is a failed run, not "
            f"an empty candidate pool."
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, (
            f"goal-selector stdout is not JSON ({exc}); bytes={len(out)} "
            f"head={out[:200]!r}"
        )
    if not isinstance(data, list):
        return None, f"expected a JSON array, got {type(data).__name__}"
    return data, None


def _noise_free(cand):
    return cand.get("score", 0.0) - (cand.get("breakdown") or {}).get(NOISE_KEY, 0.0)


def _ranks(cands, keyfn):
    """1-indexed rank map, descending by keyfn. Ties broken by goal_id so the
    ordering is TOTAL and therefore reproducible — a tie broken by list order
    would make the noise-free rank depend on selector emission order."""
    ordered = sorted(cands, key=lambda c: (-keyfn(c), c.get("goal_id") or ""))
    return {c.get("goal_id"): i + 1 for i, c in enumerate(ordered)}, ordered


def probe(cands, criterion, top=10):
    if not cands:
        return {"error": "candidate pool is empty"}

    first_bd = cands[0].get("breakdown") or {}
    if criterion not in first_bd:
        return {
            "error": f"unknown criterion {criterion!r}",
            "probeable_criteria": sorted(first_bd.keys()),
        }

    vals = [(c.get("breakdown") or {}).get(criterion, 0.0) for c in cands]
    nonzero = [v for v in vals if v]

    nf_rank, nf_ordered = _ranks(cands, _noise_free)
    obs_rank, obs_ordered = _ranks(cands, lambda c: c.get("score", 0.0))
    wo_rank, wo_ordered = _ranks(
        cands,
        lambda c: _noise_free(c) - (c.get("breakdown") or {}).get(criterion, 0.0),
    )

    moved = [g for g in nf_rank if nf_rank[g] != wo_rank[g]]
    top_with = nf_ordered[0].get("goal_id")
    top_without = wo_ordered[0].get("goal_id")

    detail = []
    for c in nf_ordered[:top]:
        gid = c.get("goal_id")
        detail.append({
            "goal_id": gid,
            "title": (c.get("title") or "")[:60],
            "score_observed": c.get("score"),
            "score_noise_free": round(_noise_free(c), 2),
            "contribution": (c.get("breakdown") or {}).get(criterion, 0.0),
            "rank_noise_free": nf_rank[gid],
            "rank_without_criterion": wo_rank[gid],
            "rank_delta": wo_rank[gid] - nf_rank[gid],
            "rank_observed": obs_rank[gid],
        })

    params = cands[0].get("exploration_params") or {}
    noise_vals = [(c.get("breakdown") or {}).get(NOISE_KEY, 0.0) for c in cands]

    return {
        "criterion": criterion,
        "composite_warning": COMPOSITE_CRITERIA.get(criterion),
        "pool_size": len(cands),
        "fires": bool(nonzero),
        "magnitude": {
            "nonzero_count": len(nonzero),
            "nonzero_pct": round(100.0 * len(nonzero) / len(cands), 1),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
            "mean": round(statistics.fmean(vals), 3),
            "median": round(statistics.median(vals), 3),
            "mean_when_nonzero": round(statistics.fmean(nonzero), 3) if nonzero else 0.0,
        },
        "rank_delta": {
            "basis": "noise-free (observed basis would be contaminated by the noise lottery)",
            "candidates_moved": len(moved),
            "candidates_moved_pct": round(100.0 * len(moved) / len(cands), 1),
            "top_pick_with_criterion": top_with,
            "top_pick_without_criterion": top_without,
            "top_pick_changes": top_with != top_without,
            "max_abs_delta": max((abs(wo_rank[g] - nf_rank[g]) for g in nf_rank), default=0),
        },
        "noise": {
            "epsilon": params.get("epsilon"),
            "noise_scale": params.get("noise_scale"),
            "noise_band_ceiling": params.get("noise_weight"),
            "observed_max": round(max(noise_vals), 3) if noise_vals else 0.0,
            "source": "per-agent, read from the selector's exploration_params (never a constant)",
            "note": (
                "a rank delta smaller than the noise band ceiling is INSIDE the "
                "lottery — the term cannot be shown to move the observed pick"
            ),
        },
        "top_detail": detail,
    }


def render_human(r):
    if "error" in r:
        lines = [f"ERROR: {r['error']}"]
        if r.get("probeable_criteria"):
            lines.append("probeable criteria: " + ", ".join(r["probeable_criteria"]))
        return "\n".join(lines)

    m, rd, nz = r["magnitude"], r["rank_delta"], r["noise"]
    out = [
        f"═══ CRITERION CONTRIBUTION: {r['criterion']} ═══",
        f"pool: {r['pool_size']} candidates (ONE selector run — guard-3562)",
        "",
        f"1. FIRES              : {'YES' if r['fires'] else 'NO'} "
        f"({m['nonzero_count']}/{r['pool_size']} = {m['nonzero_pct']}% nonzero)",
        f"2. MAGNITUDE          : min={m['min']} median={m['median']} mean={m['mean']} "
        f"max={m['max']} | mean-when-nonzero={m['mean_when_nonzero']}",
        f"3. RANK DELTA         : {rd['candidates_moved']} moved "
        f"({rd['candidates_moved_pct']}%), max |delta|={rd['max_abs_delta']}",
        f"   top pick changes   : {rd['top_pick_changes']} "
        f"({rd['top_pick_with_criterion']} -> {rd['top_pick_without_criterion']})",
        f"4. NOISE BAND         : epsilon={nz['epsilon']} x scale={nz['noise_scale']} "
        f"= ceiling {nz['noise_band_ceiling']} (observed max {nz['observed_max']})",
    ]
    if r.get("composite_warning"):
        out += ["", f"⚠ COMPOSITE CRITERION: {r['composite_warning']}"]
    if not r["fires"]:
        out += ["", "⚠ term contributes ZERO across the whole pool — it is inert HERE. "
                    "That is not the same as unwired; check scoring-criterion-audit.py "
                    "for whether its input field is populated at all."]
    elif rd["candidates_moved"] == 0:
        out += ["", "⚠ term FIRES but moves NO candidate — every contribution is uniform "
                    "across the pool, so it cancels out of the ranking entirely."]
    elif rd["max_abs_delta"] and nz["noise_band_ceiling"]:
        out += ["", f"note: {nz['note']}"]

    out += ["", f"top {len(r['top_detail'])} by noise-free rank:"]
    out.append(f"  {'goal':<13} {'nf_rank':>7} {'w/o':>5} {'delta':>6} {'contrib':>8}  title")
    for d in r["top_detail"]:
        out.append(
            f"  {d['goal_id']:<13} {d['rank_noise_free']:>7} {d['rank_without_criterion']:>5} "
            f"{d['rank_delta']:>+6} {d['contribution']:>8}  {d['title']}"
        )
    return "\n".join(out)


def self_test():
    """Offline invariant checks on synthetic candidates — no selector run, so
    this is safe and deterministic anywhere."""
    fails = []

    def mk(gid, score, bd):
        return {"goal_id": gid, "title": gid, "score": score, "breakdown": bd,
                "exploration_params": {"epsilon": 0.4, "noise_scale": 3.0,
                                       "noise_weight": 1.2}}

    # A: noise-free rank is REPRODUCIBLE and ignores the noise addend.
    # B ranks above A once noise is removed, despite a lower observed score.
    pool = [
        mk("g-a", 10.0, {"priority": 3.0, NOISE_KEY: 1.10}),
        mk("g-b", 9.5, {"priority": 3.0, NOISE_KEY: 0.05}),
    ]
    r1 = probe(pool, "priority")
    r2 = probe(pool, "priority")
    if r1["top_detail"][0]["goal_id"] != "g-b":
        fails.append("A: noise-free rank did not reorder past the noise addend")
    if [d["rank_noise_free"] for d in r1["top_detail"]] != \
       [d["rank_noise_free"] for d in r2["top_detail"]]:
        fails.append("A: noise-free rank NOT reproducible across invocations")

    # B: a uniform criterion FIRES but moves nobody (it cancels out).
    pool = [mk("g-a", 5.0, {"c": 2.0, NOISE_KEY: 0.0}),
            mk("g-b", 4.0, {"c": 2.0, NOISE_KEY: 0.0})]
    r = probe(pool, "c")
    if not r["fires"]:
        fails.append("B: uniform nonzero criterion reported as not firing")
    if r["rank_delta"]["candidates_moved"] != 0:
        fails.append("B: uniform criterion moved candidates (should cancel)")

    # C: an all-zero criterion does not fire.
    pool = [mk("g-a", 5.0, {"c": 0.0, NOISE_KEY: 0.0}),
            mk("g-b", 4.0, {"c": 0.0, NOISE_KEY: 0.0})]
    if probe(pool, "c")["fires"]:
        fails.append("C: all-zero criterion reported as firing")

    # D: a decisive criterion flips the top pick. The contribution must sit on
    # the LEADER — removing a term that benefits a TRAILING candidate makes that
    # candidate worse and correctly leaves the leader on top, so the naive
    # fixture tests nothing. (This fixture was wrong on first write and the
    # self-test caught it; keeping the reasoning here so it is not "fixed" back.)
    pool = [mk("g-a", 5.0, {"c": 4.0, NOISE_KEY: 0.0}),   # leads BECAUSE of c
            mk("g-b", 4.5, {"c": 0.0, NOISE_KEY: 0.0})]
    r = probe(pool, "c")
    if not r["rank_delta"]["top_pick_changes"]:
        fails.append("D: decisive criterion did not flip the top pick")
    if r["rank_delta"]["top_pick_without_criterion"] != "g-b":
        fails.append("D: flip went to the wrong candidate")

    # E: unknown criterion errors and lists what IS probeable.
    r = probe([mk("g-a", 1.0, {"c": 0.0, NOISE_KEY: 0.0})], "nope")
    if "error" not in r or "c" not in r.get("probeable_criteria", []):
        fails.append("E: unknown criterion did not error with a criteria list")

    # F: the composite warning is surfaced for directive_boost.
    r = probe([mk("g-a", 1.0, {"directive_boost": 1.5, NOISE_KEY: 0.0})],
              "directive_boost")
    if not r.get("composite_warning"):
        fails.append("F: directive_boost missing its composite warning")

    if fails:
        print("SELF-TEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELF-TEST PASSED: 6/6 invariants (noise-free reproducibility, "
          "uniform-cancels, zero-not-firing, decisive-flip, unknown-criterion, "
          "composite-warning)")
    return 0


def stability_check(criterion, top=25):
    """Run the selector TWICE and ATTRIBUTE any noise-free rank difference.

    Exists because the naive form of this check is a trap. Subtracting
    breakdown[exploration_noise] provably removes the noise lottery (self-test
    case A pins it on a fixed pool), but TWO other sources still move the
    noise-free rank between runs and neither is noise:

      (a) QUEUE MUTATION      — on a live multi-agent fleet, candidates enter,
                                get claimed, and complete between runs, so the
                                POOL itself differs.
      (b) TIME-DEPENDENT TERMS — recurring_urgency / deadline_urgency advance
                                with the wall clock, so a goal legitimately
                                re-scores seconds later.

    Measured 2026-08-19 (zeta, cc-02, 1301 candidates): back-to-back runs shared
    17 of 25 top rows; only 2 drifted in noise-free score (by 0.18 and 0.01) and
    the rest of the difference was pool membership. Reporting a bare
    "IDENTICAL: False" there invites the conclusion that the noise subtraction is
    broken — the exact refuted-hypothesis pattern (rb-5341, g-115-5817) this
    whole probe exists to prevent. So attribute, never a bare verdict.
    """
    runs = []
    for _ in range(2):
        cands, err = run_selector()
        if err:
            return {"error": f"stability check aborted: {err}"}
        runs.append(probe(cands, criterion, top=top))
    a, b = runs
    if "error" in a:
        return a

    ra = {d["goal_id"]: d for d in a["top_detail"]}
    rb = {d["goal_id"]: d for d in b["top_detail"]}
    common = set(ra) & set(rb)
    drift = {g: (ra[g]["score_noise_free"], rb[g]["score_noise_free"])
             for g in common
             if abs(ra[g]["score_noise_free"] - rb[g]["score_noise_free"]) > 0.001}
    reordered = [g for g in common if ra[g]["rank_noise_free"] != rb[g]["rank_noise_free"]]

    return {
        "criterion": criterion,
        "order_identical": [d["goal_id"] for d in a["top_detail"]] ==
                           [d["goal_id"] for d in b["top_detail"]],
        "pool_size_run1": a["pool_size"],
        "pool_size_run2": b["pool_size"],
        "attribution": {
            "queue_mutation": {
                "entered_in_run2": sorted(set(rb) - set(ra)),
                "left_after_run1": sorted(set(ra) - set(rb)),
                "pool_size_delta": b["pool_size"] - a["pool_size"],
            },
            "score_drift_time_dependent": {
                "common_rows": len(common),
                "drifted_rows": len(drift),
                "max_abs_drift": round(max((abs(x - y) for x, y in drift.values()),
                                           default=0.0), 3),
                "detail": {g: {"run1": x, "run2": y} for g, (x, y) in
                           sorted(drift.items())[:10]},
            },
            "reordered_common_rows": len(reordered),
        },
        "verdict": (
            "STABLE — identical order across runs (quiescent queue)"
            if [d["goal_id"] for d in a["top_detail"]] ==
               [d["goal_id"] for d in b["top_detail"]]
            else "DIFFERS — see attribution; a nonzero queue_mutation or "
                 "score_drift explains the difference WITHOUT implicating the "
                 "noise subtraction. Only an unexplained reorder (drifted_rows=0 "
                 "AND entered/left empty AND reordered_common_rows>0) would "
                 "indicate the noise removal itself is wrong."
        ),
    }


def main():
    ap = argparse.ArgumentParser(
        description="Probe a goal-selector criterion's CONTRIBUTION to the ranking.")
    ap.add_argument("--criterion", help="criterion name (breakdown key)")
    ap.add_argument("--output", choices=["json", "human"], default="human")
    ap.add_argument("--top", type=int, default=10, help="rank-delta detail rows")
    ap.add_argument("--list", action="store_true",
                    help="list probeable criteria from a live selector run")
    ap.add_argument("--self-test", action="store_true",
                    help="offline invariant checks; no selector run")
    ap.add_argument("--stability-check", action="store_true",
                    help="run the selector twice and ATTRIBUTE any noise-free "
                         "rank difference (queue mutation vs time-dependent "
                         "drift vs unexplained reorder)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.stability_check:
        if not args.criterion:
            ap.error("--stability-check requires --criterion")
        res = stability_check(args.criterion, top=args.top)
        print(json.dumps(res, indent=2))
        return 0 if "error" not in res else 2

    if not args.criterion and not args.list:
        ap.error("one of --criterion, --list, --stability-check, or --self-test is required")

    cands, err = run_selector()
    if err:
        print(json.dumps({"error": err}) if args.output == "json" else f"ERROR: {err}",
              file=sys.stderr)
        return 2

    if args.list:
        keys = sorted((cands[0].get("breakdown") or {}).keys()) if cands else []
        if args.output == "json":
            print(json.dumps({"pool_size": len(cands), "probeable_criteria": keys}, indent=2))
        else:
            print(f"{len(keys)} probeable criteria over a pool of {len(cands)}:")
            for k in keys:
                print("  ", k)
        return 0

    result = probe(cands, args.criterion, top=args.top)
    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print(render_human(result))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
