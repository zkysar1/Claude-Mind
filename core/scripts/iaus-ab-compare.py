# domain-leak-exempt: IAUS A/B comparison harness (g-306-33, BRD Gap 8). IAUS
# is the framework feature name; companion to _iaus_scorer.py + the design doc.
"""A/B comparison harness for the flag-gated IAUS goal scorer (g-306-33).

Dual-scores a fixed replay set under BOTH scorers and reports the design's
three metrics (`core/config/iaus-selector-design.md` section 4): top-1
agreement, Spearman rank correlation, and veto-correctness. The cutover gate
(g-306-33) flips `iaus_selector.use_iaus` to true ONLY on parity-or-improvement.

Determinism: exploration_noise is zeroed. The additive base for a goal is the
sum of its goal-selector `breakdown` terms EXCEPT exploration_noise (the
breakdown term `breakdown[k]` IS the production `raw[k] * WEIGHTS[k]`); the
IAUS path never uses exploration_noise (the caller adds it identically in both
branches), so dropping it isolates the scoring-shape comparison.

Replay set: the live candidate pool emitted by `goal-selector.sh` is the replay
fixture. NOTE: that pool is already COLLECT-filtered to agent-executable goals,
so the veto-by-zero improvement cannot manifest on it (nothing to veto) — which
is itself the safety check (IAUS must NOT veto any feasible goal). The veto
IMPROVEMENT is demonstrated separately on a synthetic non-executable set
(--veto-demo), mirroring the veto-by-zero unit tests in test_iaus_scorer.py.

WEIGHTS + IAUS_CONFIG are imported from goal-selector.py (the exact production
values, incl. any config-overrides) via its import-safe module body (it guards
CLI execution behind `if __name__ == "__main__"`).

Usage:
  py -3 core/scripts/iaus-ab-compare.py --fixture <selector-json> [--top-k 5] [--veto-demo]
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _iaus_scorer as I  # noqa: E402


def load_selector_consts():
    """Import goal-selector.py's WEIGHTS + IAUS_CONFIG (production-exact).

    goal-selector.py guards CLI execution behind a __main__ block, so importing
    its module body only runs the config loaders (file reads, no side effects).
    """
    spec = importlib.util.spec_from_file_location(
        "_goal_selector_for_ab", _HERE / "goal-selector.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.WEIGHTS, mod.IAUS_CONFIG


def additive_base(goal):
    """Noise-zeroed additive total = sum of breakdown terms except the noise term."""
    bd = goal.get("breakdown", {}) or {}
    return sum(v for k, v in bd.items() if k != "exploration_noise")


def spearman_rho(pos_a, pos_b):
    """Spearman's rho over goal_id -> 1-based-rank maps (distinct ranks → no ties)."""
    ids = list(pos_a.keys())
    n = len(ids)
    if n < 2:
        return 1.0
    d2 = sum((pos_a[i] - pos_b[i]) ** 2 for i in ids)
    return 1.0 - (6.0 * d2) / (n * (n * n - 1))


def score_rows(goals, weights, config):
    rows = []
    for g in goals:
        raw = g.get("raw", {}) or {}
        iz = I.iaus_score(raw, weights, config)
        rows.append({
            "goal_id": g.get("goal_id"),
            "title": (g.get("title") or "")[:46],
            "recurring": g.get("recurring", False),
            "agent_executable_raw": raw.get("agent_executable"),
            "additive": additive_base(g),
            "iaus": iz["score"],
            "iaus_veto": iz["veto"],
            "iaus_base": iz["base"],
            "iaus_pruned": iz["pruned"],
        })
    return rows


def rank_metrics(rows, top_k):
    add_rank = sorted(rows, key=lambda r: (-r["additive"], r["goal_id"]))
    iaus_rank = sorted(rows, key=lambda r: (-r["iaus"], r["goal_id"]))
    add_pos = {r["goal_id"]: i + 1 for i, r in enumerate(add_rank)}
    iaus_pos = {r["goal_id"]: i + 1 for i, r in enumerate(iaus_rank)}

    top1_add = add_rank[0]["goal_id"]
    top1_iaus = iaus_rank[0]["goal_id"]

    # veto-correctness on this set: goals additive ranked in top-K that IAUS
    # vetoed to 0, and (the safety property) any FEASIBLE goal wrongly vetoed.
    topk_add_ids = [r["goal_id"] for r in add_rank[:top_k]]
    vetoed = {r["goal_id"]: r for r in rows if r["iaus"] == 0.0}
    veto_in_topk = [
        {"goal_id": gid, "add_rank": add_pos[gid],
         "agent_executable_raw": vetoed[gid]["agent_executable_raw"],
         "correct_veto": vetoed[gid]["agent_executable_raw"] in (0, 0.0)}
        for gid in topk_add_ids if gid in vetoed
    ]
    wrongly_vetoed_feasible = [
        r["goal_id"] for r in rows
        if r["iaus"] == 0.0 and r["agent_executable_raw"] not in (0, 0.0)
    ]
    return {
        "n": len(rows),
        "top1_additive": top1_add,
        "top1_iaus": top1_iaus,
        "top1_agreement": top1_add == top1_iaus,
        "spearman_rho": round(spearman_rho(add_pos, iaus_pos), 4),
        "top_k": top_k,
        "veto_in_topk": veto_in_topk,
        "wrongly_vetoed_feasible": wrongly_vetoed_feasible,
        "add_top5": [(r["goal_id"], round(r["additive"], 2)) for r in add_rank[:5]],
        "iaus_top5": [(r["goal_id"], round(r["iaus"], 4)) for r in iaus_rank[:5]],
    }


def _veto_demo_goals():
    """Synthetic non-executable goals (agent_executable=0) with high OTHER
    criteria — the additive defect IAUS is meant to fix. The live pool is
    COLLECT-filtered so it has none; this set demonstrates the veto improvement.
    Each is a full raw dict (all axes present, 0 unless named)."""
    axes = (I.VETO_AXES + I.PRIMARY_AXES + I.MAKEUP_AXES)
    demos = [
        ("synthetic-infeasible-highpressure",
         {"agent_executable": 0, "priority": 3, "completion_pressure": 2.5,
          "recurring_urgency": 4.0}),
        ("synthetic-infeasible-deadline",
         {"agent_executable": 0, "priority": 3, "deadline_urgency": 3.0,
          "tail_bonus": 2.0}),
        ("synthetic-feasible-control",
         {"agent_executable": 2, "priority": 3, "completion_pressure": 2.5}),
    ]
    goals = []
    for gid, over in demos:
        raw = {a: 0.0 for a in axes}
        raw.update(over)
        raw["exploration_noise"] = 0.0
        # Build a breakdown so additive_base works (weight 1.0 each — the demo
        # only needs additive to rank the infeasible goals high, which it does).
        bd = {k: float(v) for k, v in raw.items() if k != "exploration_noise"}
        goals.append({"goal_id": gid, "title": gid, "recurring": False,
                      "raw": raw, "breakdown": bd})
    return goals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, help="goal-selector.sh JSON array")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--veto-demo", action="store_true",
                    help="also score a synthetic non-executable set to show the veto improvement")
    args = ap.parse_args()

    goals = json.load(open(args.fixture, encoding="utf-8"))
    if not isinstance(goals, list):
        print(json.dumps({"error": "fixture is not a goal list (all_blocked?)"}))
        return 2

    weights, config = load_selector_consts()

    live_rows = score_rows(goals, weights, config)
    report = {
        "fixture": args.fixture,
        "iaus_config": {k: config.get(k) for k in
                        ("use_iaus", "primary_floor", "watermark", "bonus_scale", "urgency_max")},
        "live_pool": rank_metrics(live_rows, args.top_k),
    }

    if args.veto_demo:
        demo_rows = score_rows(_veto_demo_goals(), weights, config)
        report["veto_demo"] = {
            "rows": [{"goal_id": r["goal_id"],
                      "agent_executable_raw": r["agent_executable_raw"],
                      "additive": round(r["additive"], 2),
                      "iaus": round(r["iaus"], 4),
                      "iaus_vetoed": r["iaus"] == 0.0} for r in demo_rows],
        }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
