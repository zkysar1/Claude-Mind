#!/usr/bin/env python3
"""defer-scope-coverage — consume the recheck lanes' exclusion counts ().

Four recheck sweeps each PRINT an exclusion count and nothing reads it:

    precondition-defer-recheck   skipped_free_form
    credential-defer-recheck     skipped_no_key
    audit-user-to-agent          undeclared user_leg_scope / unkeyable grant scope

A printed count is not a consumed one. That is the guard-1802 / reclaim-rule-7
shape: a zero-result sweep and a genuinely clean queue produce identical
output, so nobody can tell whether the lane is clean or blind. This script is
the consumer — it classifies every excluded defer through the SHARED
`gates/defer_scope` vocabulary and reports three numbers per lane:

    keyable    — the shared vocabulary DOES recognize this defer today.
                 These are the migration wins: a scope field could be
                 backfilled and the lane's sweep would then reach them.
    unkeyable  — still SENTINEL. Reported WITH the observed text, because the
                 value is the diagnostic and a count is not (step 5 of the
                 establish-vocabulary-contract pattern).
    total      — the lane's excluded population.

It is REPORT-ONLY by design. It never writes a scope onto a goal: inferring a
scope from prose and then storing it as though it were declared would launder
a guess into a fact, and `reclaim-routed-work.md` rule 2 is explicit that a
well-formed field is not a valid one. Backfilling is a human/agent decision
per goal, and the report is its input.

Exit codes:
  0  every lane fully keyable (or no excluded population at all)
  1  at least one lane still has unkeyable defers  (use --exit-on-unkeyable)
  2  could not read the queue
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "gates"))

import defer_scope  # noqa: E402
from _paths import WORLD_DIR, agents_root  # noqa: E402

# Which lane a non-terminal goal's defer belongs to. Deliberately mirrors the
# sweeps' own eligibility, so a lane's population here is the same population
# its sweep excluded — a coverage report over a DIFFERENT population would be
# the very predicate-mismatch defect this whole cluster is about (guard-1802).
TERMINAL = {"completed", "skipped", "expired"}

# A lane whose INPUT is not defer prose will score low on recognition no matter
# how good the patterns are, and a bare "0 keyable" reads like a vocabulary
# failure. Say which lanes those are, so the next reader tunes the right thing —
# or, better, tunes nothing. Widening the patterns to chase this number would be
# guard-2950 (broadening a criterion to reach a figure it lands under) and would
# also make recognition fire on unrelated titles fleet-wide.
LANE_INPUT_CAVEAT = {
    "user-leg": ("this lane's goals usually carry NO defer_reason — the text scored "
                 "here is the TITLE, so weak recognition is expected and is not a "
                 "pattern defect. The remedy is a DECLARED user_leg_scope on each "
                 "goal (g-115-3856's population work), not better regexes."),
    # THIS LANE IS NOT MEASURED BY THIS CONSUMER, and its zero must say so.
    # `_lane_of` returns exactly four values — user-leg, credential, precondition,
    # unrouted — so NOTHING can ever land in `grant`. It appears here only because
    # the lane set is built from defer_scope.lanes(), which declares the full
    # vocabulary. An earlier version of this string claimed the opposite ("an empty
    # population here means no grant currently lacks a keyable scope head, not that
    # the lane is unchecked"), which is exactly backwards and is the guard-1760
    # failure this file exists to avoid — arriving in its subtlest form. A lane
    # dropped OUTRIGHT invites "where is grant?"; a lane displaying 0 beside a
    # reassuring note answers that question before it is asked. Presence made the
    # gap harder to see, not easier. Its input is standing-grant scope PROSE, which
    # lives in a different store than the goal queue this consumer reads, so
    # measuring it is a separate piece of work — not a missing regex.
    "grant": ("NOT MEASURED BY THIS CONSUMER — no predicate here assigns to this "
              "lane, so its 0 is not evidence of a clean lane. Grant scopes live in "
              "the standing-grants prose, not in the goal queue this script reads. "
              "Read this zero as 'unexamined', never as 'clean'."),
    "unrouted": ("these defers name nothing that identifies even their LANE, so no "
                 "lane's sweep can reach them and no vocabulary could key them as "
                 "written. This bucket is the floor of the problem, not a routing "
                 "bug — read it before reading any lane's ratio."),
}


def _iter_goals(paths):
    for p in paths:
        try:
            fh = open(p, encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    asp = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(asp, dict):
                    continue
                for g in (asp.get("goals") or []):
                    if isinstance(g, dict):
                        yield g


def _lane_of(goal):
    """Which lane's exclusion population this goal sits in, or None.

    A goal can only be in ONE lane here — the report counts populations, and
    double-counting one goal across two lanes would inflate every ratio.
    Order is deliberate: the user-leg lane is checked first because it is the
    only one keyed off `participants` rather than off defer prose.
    """
    if goal.get("status") in TERMINAL:
        return None
    parts = goal.get("participants")
    if isinstance(parts, list) and "user" in parts and not goal.get("user_leg_scope"):
        return "user-leg"
    defer = goal.get("defer_reason")
    if not defer:
        return None
    low = str(defer).lower()
    # credential lane: the sweep's own eligibility is a human_blocked defer it
    # then tries to key to an env var.
    if "credential" in low or "env-read" in low or "iam" in low or "denied" in low:
        return "credential"
    if low.startswith("precondition_unmet") or "precondition" in low:
        return "precondition"
    # UNROUTED, and deliberately NOT defaulted into `precondition`.
    #
    # Routing by keyword uses the very prose whose un-keyability is the subject
    # of this report, so a defer that names nothing recognizable cannot be
    # lane-assigned by text at all. An earlier draft defaulted these to
    # `precondition`, which was circular and confidently wrong in one
    # direction: it inflated precondition's unkeyable count with defers that
    # were never precondition defers, and deflated every other lane's. Caught
    # by test_consumer_splits_the_exclusion_count_into_keyable_and_unkeyable,
    # which expected a credential-lane miss and got a precondition-lane one.
    #
    # `unrouted` is the stronger finding, not a gap in the report: a defer so
    # free-form that even its LANE is unknown is further from keyable than one
    # that lands in a lane and misses the vocabulary.
    return "unrouted"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--output", choices=["text", "json"], default="text")
    ap.add_argument("--exit-on-unkeyable", action="store_true",
                    help="exit 1 when any lane still has unkeyable defers")
    ap.add_argument("--show", type=int, default=5,
                    help="how many unkeyable excerpts to show per lane (0 = all)")
    ap.add_argument("--queue", action="append", default=[],
                    help="TEST SEAM: explicit aspirations.jsonl path(s)")
    args = ap.parse_args()

    if args.queue:
        paths = [Path(q) for q in args.queue]
    else:
        paths = [Path(WORLD_DIR) / "aspirations.jsonl"]
        try:
            for conf_dir in sorted(agents_root().glob("*/")):
                cand = conf_dir / "aspirations.jsonl"
                if cand.is_file():
                    paths.append(cand)
        except Exception:
            pass  # world queue alone is a valid, smaller report

    if not any(p.is_file() for p in paths):
        print(json.dumps({"error": "no readable queue", "paths": [str(p) for p in paths]}))
        return 2

    # `unrouted` is a REPORTED bucket, not a dropped one. Skipping it would be
    # a silent truncation of exactly the population this report exists to
    # measure, and a shrinking denominator reads as improving coverage
    # (guard-1760: a report must name what it declined to count).
    lanes = {ln: {"total": 0, "keyable": 0, "unkeyable": 0, "by_token": {}, "samples": []}
             for ln in list(defer_scope.lanes()) + ["unrouted"]}

    for g in _iter_goals(paths):
        lane = _lane_of(g)
        if lane is None or lane not in lanes:
            continue
        text = g.get("defer_reason") or g.get("title") or ""
        token = defer_scope.classify(lane, text)
        row = lanes[lane]
        row["total"] += 1
        if token == defer_scope.SENTINEL:
            row["unkeyable"] += 1
            u = defer_scope.undeclared(lane, text)
            if u:
                u["goal_id"] = g.get("id")
                # For an UNROUTED defer, say whether the scope itself is
                # recognizable even though the lane is not. "recognizable scope,
                # unknown lane" and "recognizable as nothing" need different
                # remedies, and a bare unrouted count cannot tell them apart.
                if lane == "unrouted":
                    anytok = defer_scope.classify_any(text)
                    u["lane_agnostic_token"] = anytok
                    if anytok != defer_scope.SENTINEL:
                        row["scope_recognizable"] = row.get("scope_recognizable", 0) + 1
                row["samples"].append(u)
        else:
            row["keyable"] += 1
            row["by_token"][token] = row["by_token"].get(token, 0) + 1

    total_unkeyable = sum(v["unkeyable"] for v in lanes.values())
    result = {
        "lanes": {},
        "total_excluded": sum(v["total"] for v in lanes.values()),
        "total_keyable": sum(v["keyable"] for v in lanes.values()),
        "total_unkeyable": total_unkeyable,
        "sentinel": defer_scope.SENTINEL,
        "report_only": True,
    }
    for ln, v in lanes.items():
        samples = v["samples"] if args.show == 0 else v["samples"][:args.show]
        result["lanes"][ln] = {
            "total": v["total"], "keyable": v["keyable"], "unkeyable": v["unkeyable"],
            "by_token": dict(sorted(v["by_token"].items())),
            "unkeyable_samples": samples,
        }
        if "scope_recognizable" in v:
            result["lanes"][ln]["scope_recognizable"] = v["scope_recognizable"]
        if ln in LANE_INPUT_CAVEAT:
            result["lanes"][ln]["input_caveat"] = LANE_INPUT_CAVEAT[ln]

    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        print("defer-scope coverage — %d excluded defer(s) across %d lane(s)"
              % (result["total_excluded"], len(lanes)))
        for ln in sorted(lanes):
            v = result["lanes"][ln]
            if not v["total"]:
                # A zero has two causes and they are NOT interchangeable: the lane
                # was measured and came back empty, or nothing can reach it at all.
                # Rendering both as "(no excluded population)" makes an unexamined
                # lane read as a clean one, which is the guard-1760 failure this
                # file otherwise guards against. UNMEASURED lanes are the ones whose
                # caveat opens with the marker below — keep that the single source
                # of truth so a new unmeasured lane cannot be added without one.
                unmeasured = LANE_INPUT_CAVEAT.get(ln, "").startswith("NOT MEASURED")
                print("  %-13s %s" % (ln, "UNEXAMINED — no predicate assigns to this "
                                          "lane; this 0 is not a clean result"
                                          if unmeasured else "(measured, no excluded population)"))
                if unmeasured:
                    print("      NOTE: %s" % LANE_INPUT_CAVEAT[ln])
                continue
            print("  %-13s total=%-4d keyable=%-4d unkeyable=%-4d  %s"
                  % (ln, v["total"], v["keyable"], v["unkeyable"],
                     ", ".join("%s=%d" % kv for kv in v["by_token"].items()) or "-"))
            if v.get("scope_recognizable"):
                print("      of which %d have a RECOGNIZABLE scope but no identifiable lane"
                      % v["scope_recognizable"])
            for s in v["unkeyable_samples"]:
                tok = s.get("lane_agnostic_token")
                suffix = "" if not tok or tok == defer_scope.SENTINEL else "  [scope~%s]" % tok
                print("      UNKEYABLE %-12s %s%s" % (s.get("goal_id"), s["observed"], suffix))
            if ln in LANE_INPUT_CAVEAT:
                print("      NOTE: %s" % LANE_INPUT_CAVEAT[ln])
        print("REPORT ONLY — no scope is written to any goal. Backfill is a per-goal "
              "decision (reclaim-routed-work r2: well-formed is not valid).")

    if args.exit_on_unkeyable and total_unkeyable:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
