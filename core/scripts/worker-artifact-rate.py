#!/usr/bin/env python3
"""Learning-artifact production rate for WORKER-completed goals ().

THE QUESTION: of the non-routine goals a worker Body completed, what fraction
produced at least one learning artifact? The worker path skips every
reducer-only encode phase, so its learning reaches a store only through the four
capture lanes and the reducer's replay of them. If that bridge silently stops
working, worker goals keep closing and nothing is encoded — and the failure is
invisible, because a store that receives nothing looks exactly like a store with
nothing to receive (rb-8477).

THE POPULATION KEY. `completed_by_role == "worker"`, the stamp added by this
same goal. Read its asymmetry before trusting any number here:

    PRESENT + "worker"  -> positively a worker close
    ABSENT              -> reducer OR unknown, NEVER "reducer"

`bash-agent-inject.py` exports BODY_ROLE only on the worker fork path, so the
reducer never sets it and the stamp is deliberately not written rather than
guessed. Everything below therefore reports the unstamped group as
"unstamped (reducer-or-unknown)" and never as "reducer". The soak gate's "zero
regression in reducer artifact rate" cannot be measured from this field alone;
the comparison printed here is a floor, not that metric.

WHY THERE IS AN INSUFFICIENT-DATA STATE. The stamp is going-forward, so the
population is 0 the day it ships and grows only as workers close goals on boxes
whose daemon has restarted (the allowlist is imported at daemon start). A rate
computed over N=0 is not 0% and is not 100% — it is unmeasured, and printing
PASS or FAIL for it would be exactly the vacuous-zero failure this check exists
to detect (guard-4093: a zero with a blind lane is UNREACHABLE, not EMPTY). So
below --min-sample the check reports INSUFFICIENT DATA and exits 0 without
claiming a verdict.

ATTRIBUTION. A goal counts as having produced a learning artifact when its
goal_id appears as:
    reasoning-bank  origin_goal_id  (1,653 of 8,402 records carry one)
    reasoning-bank  source          (81)
    guardrails      source          (3,719 of 4,480 are goal ids)
This is a FLOOR, not the full artifact set: tree nodes and experience files are
not indexed by goal_id in a form this can join on, so a goal that produced only
a tree node reads here as producing nothing. Under-counting is the safe
direction for a detector (it cannot manufacture a passing rate), but never quote
this rate as "the" artifact rate without that caveat.

Usage:
    py -3 core/scripts/worker-artifact-rate.py [--threshold 0.60]
                                               [--min-sample 10] [--json]
Exit: 0 = PASS or INSUFFICIENT DATA, 1 = FAIL (rate below threshold), 2 = setup.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GOAL_ID_RE = re.compile(r"^g-\d+-\d+$")


def _load_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _artifact_goal_ids(world: Path) -> set:
    """Goal ids that produced at least one reasoning-bank entry or guardrail."""
    ids = set()
    for rec in _load_jsonl(world / "reasoning-bank.jsonl"):
        for key in ("origin_goal_id", "source"):
            val = str(rec.get(key) or "")
            if GOAL_ID_RE.match(val):
                ids.add(val)
    for rec in _load_jsonl(world / "guardrails.jsonl"):
        val = str(rec.get("source") or "")
        if GOAL_ID_RE.match(val):
            ids.add(val)
    return ids


def _goal_id(goal: dict) -> str:
    """The goal's id, under either of the two names it travels under.

    THE RAW STORE USES `id`. `goal_id` is the name `aspirations-query.sh`'s
    default PROJECTION emits, and reading the projection's name against the raw
    store yields None for every record — a silent, confident zero with no error
    anywhere (guard-4024, the id-vs-goal_id trap). This cost a wrong reading
    during this very script's first run: the artifact index held 3,195 goal ids
    and 636 completed non-routine goals were scanned, and the intersection came
    back EMPTY. Both names are accepted here precisely because both shapes are
    real and a future caller may hand us either.
    """
    return str(goal.get("id") or goal.get("goal_id") or "")


def _completed_goals(world: Path):
    """(goal_id, completed_by_role, outcome_class) for every completed goal."""
    for rec in _load_jsonl(world / "aspirations.jsonl"):
        for goal in rec.get("goals", []) or []:
            if goal.get("status") != "completed":
                continue
            yield (_goal_id(goal),
                   str(goal.get("completed_by_role") or ""),
                   str(goal.get("outcome_class") or ""))


def measure(world: Path) -> dict:
    artifacts = _artifact_goal_ids(world)
    worker, unstamped = [], []
    total_completed = 0
    for gid, role, outcome in _completed_goals(world):
        total_completed += 1
        if outcome == "routine":
            continue          # the soak gate scopes to NON-routine goals
        (worker if role == "worker" else unstamped).append(gid)

    def rate(group):
        if not group:
            return None
        return sum(1 for g in group if g in artifacts) / len(group)

    return {
        "artifact_index_size": len(artifacts),
        "total_completed_goals": total_completed,
        "worker_population": len(worker),
        "worker_with_artifact": sum(1 for g in worker if g in artifacts),
        "worker_rate": rate(worker),
        # NEVER call this "reducer" — absent means reducer-OR-unknown.
        "unstamped_population": len(unstamped),
        "unstamped_with_artifact": sum(1 for g in unstamped if g in artifacts),
        "unstamped_rate": rate(unstamped),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.60,
                    help="soak gate: fraction of non-routine worker goals that "
                         "must produce >=1 learning artifact (default 0.60)")
    ap.add_argument("--min-sample", type=int, default=10,
                    help="below this population the check reports INSUFFICIENT "
                         "DATA rather than a verdict (default 10)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        from _paths import WORLD_DIR
        world = Path(WORLD_DIR)
    except Exception as exc:                       # noqa: BLE001
        print(f"SETUP: cannot resolve WORLD_DIR: {exc}", file=sys.stderr)
        return 2
    if not (world / "aspirations.jsonl").exists():
        print(f"SETUP: no aspirations.jsonl under {world}", file=sys.stderr)
        return 2

    m = measure(world)
    n = m["worker_population"]

    # POSITIVE CONTROL — refuse to report a rate computed from a broken join.
    # Every number below is an INTERSECTION of two id sets, and the failure mode
    # of an intersection is a clean zero that looks like a measurement. If goals
    # were scanned and artifacts were indexed but NOTHING intersects, the join
    # key is wrong (that is exactly how this script's first run read 0 of 636 —
    # see _goal_id). Fail loudly at rc=2 rather than emit a confident 0%.
    if (m["total_completed_goals"] > 0 and m["artifact_index_size"] > 0
            and m["worker_with_artifact"] == 0
            and m["unstamped_with_artifact"] == 0):
        print(f"SETUP: join produced ZERO matches against "
              f"{m['artifact_index_size']} artifact ids over "
              f"{m['total_completed_goals']} completed goals — the id key is "
              f"almost certainly wrong (guard-4024: the raw store uses `id`, "
              f"the query projection uses `goal_id`). Refusing to report a rate "
              f"from a broken join.", file=sys.stderr)
        return 2

    # The unfiltered population is printed BESIDE the filtered one on every
    # path, so a 0 can never be read as "measured zero" without also seeing
    # how many goals were considered (guard-2298).
    context = (f"artifact_index={m['artifact_index_size']} "
               f"completed_goals={m['total_completed_goals']} "
               f"worker_stamped_non_routine={n} "
               f"unstamped_non_routine={m['unstamped_population']}")

    if args.json:
        m["threshold"] = args.threshold
        m["min_sample"] = args.min_sample
        m["verdict"] = ("INSUFFICIENT_DATA" if n < args.min_sample
                        else ("PASS" if m["worker_rate"] >= args.threshold
                              else "FAIL"))
        print(json.dumps(m, indent=2))
        return 0 if m["verdict"] != "FAIL" else 1

    if n < args.min_sample:
        print(f"INSUFFICIENT DATA: {n} stamped non-routine worker-completed "
              f"goal(s), need {args.min_sample} before a rate means anything. "
              f"completed_by_role is a going-forward stamp (g-306-204), so this "
              f"is the expected state until workers close goals on boxes whose "
              f"daemon has restarted. {context}")
        return 0

    pct = 100.0 * m["worker_rate"]
    unst = ("n/a" if m["unstamped_rate"] is None
            else f"{100.0 * m['unstamped_rate']:.1f}%")
    verdict = "PASS" if m["worker_rate"] >= args.threshold else "FAIL"
    print(f"{verdict}: {m['worker_with_artifact']}/{n} "
          f"({pct:.1f}%) of non-routine worker-completed goals produced >=1 "
          f"learning artifact; threshold {100.0 * args.threshold:.0f}%. "
          f"Unstamped (reducer-OR-unknown, NOT 'reducer') comparison: {unst}. "
          f"{context}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
