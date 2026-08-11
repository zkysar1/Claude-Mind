#!/usr/bin/env python3
"""Hypothesis-Terminal Goal Check — surface OPEN goals whose backing hypothesis
has already reached a terminal pipeline stage, so a goal whose question is
already answered stops competing for selector attention.

THE GAP THIS FILLS. A goal carrying `hypothesis_id` stays `pending` after its
hypothesis resolves, and nothing closes it:

  * `hypothesis-discovered-overdue-sweep.py` handles the INVERSE case — records
    orphaned in stage=discovered past their deadline. It never looks at goals.
  * ONE mechanism does close goals on hypothesis stage, and it is NOT this
    population. `recover-recurring` Case 3 (`aspirations_write.py` ~L6055,
    g-115-236) retires a goal whose hypothesis reached `archived`. Its three
    filters bound it tightly, and this sweep's live population misses ALL THREE
    independently (measured 2026-08-10 over 31 hits):
        `if not g.get("recurring"): continue`      -> 0 of 31 are recurring
        `h.get("stage") == "archived"`             -> 28 of 31 are `resolved`
        `status not in ("pending","completed")`    -> 27 of 31 are `in-progress`
    So the two are disjoint, not redundant. State it this way rather than as
    "nothing closes on hypothesis stage" — that flat absence claim is what an
    earlier draft of this docstring said, and it is wrong in a way that would
    have sent the next reader looking for a mechanism they'd be told didn't
    exist. Case 3's own scoping is what makes this sweep necessary.
  * `goal-selector.py` reads `hypothesis_id` for SCORING only (`evidence_score`,
    ~L2739) — it never writes status.
  * The precheck Hypothesis Expiration Check fires on DATE (now > resolves_by
    -> status=expired), which is both late AND wrong-labelled: a resolved
    hypothesis's goal is DONE, not unresolvable, and `expired` is exempt from
    accuracy stats, so the mislabel is silently lossy.

MEASURED INSTANCES (all three closed by hand, none by a sweep):
  g-318-57   hyp resolved CORRECTED 2026-07-25; scored #5 of 171 the NEXT day
             and was claimed before the staleness was noticed.
  g-115-1983 hyp archived CONFIRMED 2026-07-16, reflected=true; sat 10 days.
             Its resolves_by was 2026-08-15, so date-expiry would not have
             touched it for three more weeks.
  g-115-3668 hyp resolved CORRECTED 2026-07-28; sat FIVE DAYS and then scored
             **rank 1 of 584** on 2026-08-02. It is HIGH priority, and priority
             is a scoring input — so the longer such a goal survives, the more
             selector attention it consumes. This is worse than sitting unread.

A fourth shape is a 6-MINUTE cross-agent race rather than multi-day drift
(g-335-626, 2026-07-31): `resolves_no_earlier_than` CONCENTRATES the race —
the gate opens at one timestamp, every agent's selector becomes eligible
simultaneously, and whichever agent resolves the hypothesis directly (usually
its filer, working its own lane) does so within minutes while the resolution
GOAL may be held by anyone. That inverts any point-in-time population estimate:
a snapshot samples the slow tail and systematically under-reports a race that
resolves itself inside one iteration.

DETECTIVE, NOT CORRECTIVE — deliberately no `--apply`, and the reason is
stronger than "the population is small".

  Closing on `stage in (resolved, archived)` ALONE WOULD DROP REAL WORK.
  Measured on g-115-3668: it carried a second obligation beyond its hypothesis
  ("if it resolves CORRECTED ... the other two open register rows should be
  re-read in that light"). That residual was real and was satisfied by a
  DIFFERENT goal, so verifying it took reading the Permission-Grant Register —
  not the hypothesis record. An auto-close keyed only on hypothesis stage would
  have shut the goal while that clause was outstanding, and nothing would have
  surfaced it again.

  So the sweep SURFACES and lets a reader decide. `residual_scope_suspected`
  below is a PROMPT TO READ, never a determination (guard-2028: a detective
  sweep's flagged entry is a hypothesis, not a finding).

LANE ROUTING (guard-1007). A hit whose `intended_agent` is another agent gets
`action="board-post"`, never a close — closing another agent's goal appropriates
their queue. `action` is computed here rather than left to the reader precisely
because the reader is the one who forgets.

STATELESSNESS IS THE KNOWN COST (guard-1826). This sweep re-surfaces the same
hits every iteration, to every agent, until the underlying goal changes. A hit
is evidence that a condition HOLDS, never evidence that it is UNREPORTED. Two
things make that cheap to live with rather than noisy:
  * `days_since_outcome` is reported, so a long-standing hit is visibly old
    rather than looking new on every read.
  * `action` is precomputed, so acting on a hit costs no re-derivation.
Before filing any GOAL about a hit, query the queue by the goal id first — the
guard-1826 / guard-2177 discipline applies to this sweep's output like any
other.

FAIL-OPEN, AND LOUD ABOUT IT. Verification criterion 5 for this work is
"unreadable pipeline or goal source yields fewer hits, never an exception",
which pulls against guard-383 (a source read error is normally FATAL, because a
silent empty aggregate writes a complete-looking lie). Both are honored by
catching the error and REPORTING it: `pipeline_read_failed`,
`goal_read_failed`, and `degraded` are in the JSON, so a zero produced by a
failed read is never mistakable for a clean queue. Reporting what was NOT
looked at is the whole point (guard-1760 / guard-1715 — an enumerator's
all-clear is bounded by the population it declares).

Verdicts:
  hypothesis_terminal — hypothesis stage in (resolved, archived); goal open.
                        Read `outcome` / `outcome_date` / `reflected` before
                        acting; an UNREFLECTED terminal hypothesis may still
                        owe reflection work.
  hypothesis_dangling — `hypothesis_id` resolves to no record in ANY stage. It
                        can never auto-clear, so it sits forever unless someone
                        repoints or removes the reference.
  hypothesis_live     — hypothesis still discovered/active/measurement-pending.
                        NOT reported (the quiet case).

JSON output:
  {
    "scanned": N,                  # open goals examined
    "with_hypothesis": N,          # of those, carrying a hypothesis_id
    "terminal_count": N,
    "hypothesis_terminal": [entry, ...],
    "hypothesis_dangling": [entry, ...],
    "pipeline_index_size": N,
    "pipeline_stages_read": {stage: count},
    "pipeline_read_failed": [stage, ...],
    "goal_read_failed": [source, ...],
    "degraded": bool,              # true if ANY read failed — a 0 is not clean
    "now": iso
  }

Sibling pattern: blocked-signal-resolution-check.py (precheck 0.5b.12), whose
detective-only call this one follows for the same reasons. Guards honored:
guard-420 (tolerant datetime parse), guard-645 (every field read via .get with
a default), guard-614 (structured JSON output), guard-365 (bash wrapper),
guard-1007 (lane routing), guard-2028 (flag is a hypothesis, not a finding),
guard-1826 (stateless re-surfacing), guard-1760 (report what was not scanned).
Reference: g-115-3355.
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _dt import parse_naive_iso  # noqa: E402
import _rt  # noqa: E402

# Terminal pipeline stages. `resolved` is the live holding area; records migrate
# to `archived` as they age, so a resolved-only test is a SURVIVORSHIP FILTER
# that would miss the older — i.e. stalest — half of the population outright.
# 's hypothesis was ARCHIVED, so a resolved-only sweep misses the
# very instance that motivated this script.
TERMINAL_STAGES = ("resolved", "archived")
LIVE_STAGES = ("discovered", "active", "measurement-pending")

OPEN_STATUSES = ("pending", "in-progress")


def _parse_iso(ts):
    """Tolerant ISO parse (guard-420). Returns datetime or None — never raises."""
    if not ts:
        return None
    try:
        return parse_naive_iso(ts)
    except Exception:
        return None


def _read_pipeline_stage(stage):
    """Return (records, ok) for ONE pipeline stage.

    Fail-open per criterion 5: on error return ([], False) so the caller can
    record the stage in `pipeline_read_failed` and set `degraded`. Never raises,
    never exits — but never silent either.
    """
    try:
        out = _rt.rt_call("GET", "/v1/pipeline/read",
                          query="stage=%s" % _rt._q(stage))
    except Exception as e:  # noqa: BLE001 — deliberate catch-all, see docstring
        print("[hypothesis-terminal-goal-check] pipeline stage %s read failed: %s"
              % (stage, e), file=sys.stderr)
        return ([], False)
    try:
        data = _rt.tolerant_decode_aggregate(
            "hypothesis-terminal-goal-check: pipeline %s" % stage, out)
    except SystemExit:
        # tolerant_decode_aggregate exits on a malformed body (guard-383). This
        # sweep must degrade rather than die, so convert it to a reported miss.
        print("[hypothesis-terminal-goal-check] pipeline stage %s undecodable"
              % stage, file=sys.stderr)
        return ([], False)
    if data is None:
        return ([], True)  # empty stage is a valid state, not a failure
    recs = data.get("records") if isinstance(data, dict) else data
    return ([r for r in (recs or []) if isinstance(r, dict)], True)


def _read_goals(source):
    """Return (goals, ok) for ONE queue. Fail-open + reported (see docstring)."""
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except Exception as e:  # noqa: BLE001
        print("[hypothesis-terminal-goal-check] %s read failed: %s"
              % (source, e), file=sys.stderr)
        return ([], False)
    try:
        data = _rt.tolerant_decode_aggregate(
            "hypothesis-terminal-goal-check: %s" % source, out)
    except SystemExit:
        print("[hypothesis-terminal-goal-check] %s undecodable" % source,
              file=sys.stderr)
        return ([], False)
    if data is None:
        return ([], True)
    asps = data.get("aspirations") if isinstance(data, dict) else data
    goals = []
    for asp in asps or []:
        if not isinstance(asp, dict):
            continue
        for g in asp.get("goals", []) or []:
            if not isinstance(g, dict):
                continue
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return (goals, True)


# Phrases that mark a CONDITIONAL FOLLOW-ON OBLIGATION — work the goal owes
# depending on how its hypothesis lands. Not a topic list and not a synonym
# list: each encodes "and then something else must happen", which is the only
# thing that makes a goal unsafe to close on hypothesis stage alone.
#
# MEASURED against the live queue before adoption, because a prose predicate
# that fires on everything is indistinguishable from no predicate (guard-2860 —
# never relax an ownership/eligibility test into a pattern). On 1,733 open
# goals: 58 fire (3.3%) for the conditional set; among the 150 open goals
# carrying a hypothesis — the only population this flag is ever consulted for —
# 1 fires, plus 3 more from the consumer set below. Two further candidates were
# dropped for firing on incidental prose rather than on obligation ("the other
# two" 32 hits, "more consequential" rhetorical).
RESIDUAL_MARKERS = (
    "if it resolves", "if this resolves", "in that light",
    "should be re-read", "re-read in", "should also", "should then",
    "rather than assumed",
    # The DOWNSTREAM-CONSUMER shape, added after the first live run surfaced
    # exactly two goals in this agent's own lane and BOTH carried one. A
    # resolution goal that names a consumer ("Consumer: hypothesis-calibration
    # node (inversion section retract-or-confirm)") owes that consumer an
    # update; the hypothesis reaching `resolved` settles the question but not
    # the propagation. Measured: 0.8% of open goals, 2.0% of open goals
    # carrying a hypothesis — so this is a real convention in resolution-goal
    # descriptions, not template boilerplate that would fire on everything.
    "consumer:", "consumers:", "cross-check against",
)


def _residual_scope_suspected(goal):
    """Heuristic PROMPT (never a determination) that a goal owes work beyond its
    hypothesis. Returns (suspected, n_outcomes, matched_markers).

    Motivated by g-115-3668, whose second obligation was satisfied by a
    DIFFERENT goal entirely — so an auto-close keyed on hypothesis stage would
    have dropped it. Deliberately biased toward suspicion: a false suspicion
    costs one extra read; a false all-clear drops real work.

    TWO SIGNALS, because the first one alone MISSED ITS OWN MOTIVATING CASE.
    The obvious test — "more than one verification outcome" — is the one I
    wrote first, and g-115-3668 has exactly ONE outcome. Its residual sits in
    DESCRIPTION PROSE ("the other two open register rows should be re-read in
    that light rather than assumed narrow"), which is where a conditional
    obligation naturally gets written, since it is not yet a commitment at
    filing time. A structural count cannot see prose, so the count-only flag
    would have shipped reading like coverage while blind to the exact shape it
    was built for — worse than no flag at all.
    """
    ver = goal.get("verification") or {}
    outcomes = ver.get("outcomes") if isinstance(ver, dict) else None
    n = len(outcomes) if isinstance(outcomes, list) else 0

    blob = " ".join([
        goal.get("description") or "",
        goal.get("title") or "",
    ] + [str(x) for x in (outcomes or [] if isinstance(outcomes, list) else [])]
    ).lower()
    matched = [m for m in RESIDUAL_MARKERS if m in blob]

    return (n > 1 or bool(matched), n, matched)


def _classify(goal, stage_index, now, self_agent, stage_conflicts=None):
    """Pure eligibility test for ONE goal. Returns an entry dict or None.

    Pure (no I/O, no daemon) so the verdict ladder is unit-testable with
    synthetic goals — the reads in main() are the only impure part.
    """
    if goal.get("status") not in OPEN_STATUSES:
        return None
    hyp_id = goal.get("hypothesis_id")
    if not hyp_id or not isinstance(hyp_id, str) or not hyp_id.strip():
        return None
    hyp_id = hyp_id.strip()

    rec = stage_index.get(hyp_id)
    if rec is None:
        verdict = "hypothesis_dangling"
        stage = None
    else:
        stage = rec.get("stage")
        if stage in TERMINAL_STAGES:
            verdict = "hypothesis_terminal"
        else:
            return None  # hypothesis_live — the quiet case, never reported

    intended = goal.get("intended_agent")
    claimed_by = goal.get("claimed_by")

    # OWNERSHIP IS `claimed_by`, NOT `intended_agent` — and reading the wrong
    # one here is the whole failure this ordering exists to prevent. Measured
    # while building this sweep:  and  both carry
    # `intended_agent: either` (so an intended-only test says "mine, close it")
    # while `claimed_by: alpha` says alpha is executing them RIGHT NOW. An
    # intended-only predicate routes THIS agent to close a partner's live work
    # — precisely the queue appropriation guard-1007 forbids, arrived at from
    # the one direction that reads as correct.
    #
    # `intended_agent` answers "who SHOULD do this"; `claimed_by` answers "who
    # IS doing this". A live claim outranks a routing preference, so it is
    # tested first and its verdict is never overridden below.
    if claimed_by and claimed_by != self_agent:
        lane, action = "claimed-by-other", "board-post"
    elif intended and intended not in ("either", "", self_agent):
        # `either` / unset are claimable by anyone, so they are MINE to act on.
        # A named OTHER agent is theirs — surface for a board post, never close.
        lane, action = "other", "board-post"
    else:
        lane, action = "mine", "review-and-close"
    if verdict == "hypothesis_dangling":
        # A dangling reference is never a close candidate — the goal's real
        # state is unknown, and the reference itself is the defect.
        action = "repoint-or-remove-reference"

    residual, n_outcomes, residual_markers = _residual_scope_suspected(goal)

    outcome_date = (rec or {}).get("outcome_date")
    od = _parse_iso(outcome_date)
    # `outcome_date` is DATE-granular (2026-08-09, no time), so this figure is
    # date-level too: a hypothesis resolved earlier today reads ~0.4d, never 0.
    # Do not read a sub-day value as a live race.
    days_since = round((now - od).total_seconds() / 86400, 1) if od else None

    ca = _parse_iso(goal.get("claimed_at"))
    claim_age_h = round((now - ca).total_seconds() / 3600, 1) if ca else None

    return {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "intended_agent": intended,
        "claimed_by": claimed_by,
        "claimed_at": goal.get("claimed_at"),
        "claim_age_hours": claim_age_h,
        "priority": goal.get("priority"),
        "status": goal.get("status"),
        "title": (goal.get("title") or "")[:80],
        "verdict": verdict,
        "hypothesis_id": hyp_id,
        "hypothesis_stage": stage,
        "outcome": (rec or {}).get("outcome"),
        "outcome_date": outcome_date,
        "days_since_outcome": days_since,
        "reflected": (rec or {}).get("reflected"),
        "resolved_by": (rec or {}).get("resolved_by"),
        "lane": lane,
        "action": action,
        "residual_scope_suspected": residual,
        "verification_outcome_count": n_outcomes,
        "residual_markers": residual_markers,
        # Non-null only when this hypothesis id was found in more than one
        # stage. The verdict above used the terminal one; this says so, so a
        # reader can tell a collapsed ambiguity from a clean lookup.
        "stage_conflict": (stage_conflicts or {}).get(hyp_id),
    }


def main():
    ap = argparse.ArgumentParser(
        description=("Surface OPEN goals whose backing hypothesis has already "
                     "reached a terminal pipeline stage. Detective only — "
                     "never mutates."),
    )
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args()

    now = dt.datetime.now()
    self_agent = os.environ.get("MIND_AGENT") or ""

    # ONE pass over the pipeline, indexed by id — the cheap-detection shape the
    # goal specifies. Every stage is read so a record's absence from the index
    # genuinely means "in no stage", which is what makes `hypothesis_dangling`
    # decidable rather than an artifact of a partial read.
    stage_index = {}
    id_stages = {}
    stages_read = {}
    pipeline_read_failed = []
    for stage in TERMINAL_STAGES + LIVE_STAGES:
        recs, ok = _read_pipeline_stage(stage)
        if not ok:
            pipeline_read_failed.append(stage)
            continue
        stages_read[stage] = len(recs)
        for r in recs:
            rid = r.get("id")
            if not rid:
                continue
            id_stages.setdefault(rid, []).append(stage)
            # TERMINAL-FIRST IS DELIBERATE, and the iteration order above is the
            # only thing enforcing it. A record present in two stages resolves
            # to the terminal one, which fails toward SURFACING: the cost is one
            # read of a goal that turns out to be live. Live-first would fail
            # toward SILENCE — the goal sits forever, which is the entire defect
            # this sweep exists to fix. Not hypothetical: 3 ids sat in two
            # stages on 2026-08-10, one in `archived`+`discovered`.
            if rid not in stage_index:
                stage_index[rid] = r
    # Report the ambiguity rather than only resolving it. Picking a winner
    # silently would make a genuine data anomaly indistinguishable from a clean
    # lookup (guard-2448 — a sweep's count is not a description of its
    # population until you know what it collapsed).
    stage_conflicts = {k: v for k, v in id_stages.items() if len(v) > 1}

    all_goals = []
    goal_read_failed = []
    for src in ("world", "agent"):
        goals, ok = _read_goals(src)
        if not ok:
            goal_read_failed.append(src)
            continue
        all_goals.extend(goals)

    buckets = {"hypothesis_terminal": [], "hypothesis_dangling": []}
    with_hypothesis = 0
    for g in all_goals:
        if g.get("status") in OPEN_STATUSES and g.get("hypothesis_id"):
            with_hypothesis += 1
        entry = _classify(g, stage_index, now, self_agent, stage_conflicts)
        if entry is None:
            continue
        buckets[entry["verdict"]].append(entry)

    for v in buckets.values():
        v.sort(key=lambda e: (e["days_since_outcome"] is None,
                              -(e["days_since_outcome"] or 0)))

    degraded = bool(pipeline_read_failed or goal_read_failed)
    result = {
        "scanned": len(all_goals),
        "with_hypothesis": with_hypothesis,
        "terminal_count": len(buckets["hypothesis_terminal"]),
        "hypothesis_terminal": buckets["hypothesis_terminal"],
        "hypothesis_dangling": buckets["hypothesis_dangling"],
        # WHAT THIS RUN COULD SEE — declared, not implied (guard-2529: a sweep
        # that filters its input before counting must report what the filter
        # EXCLUDED). `agent` resolves to the BOUND agent's queue only, so a
        # zero here is "clean for world + <this agent>", never "clean fleet-wide";
        # a sibling's private queue is invisible until that sibling runs the
        # sweep. Measured 2026-08-10: 0 open goals across 357 ARCHIVED
        # aspirations, so excluding the archive costs nothing on live data —
        # recorded here so the next reader does not re-derive it.
        "sources_scanned": ["world", "agent:%s" % (self_agent or "<unbound>")],
        "scan_bound": ("world queue + the bound agent's queue; other agents' "
                       "private queues and archived aspirations are NOT scanned"),
        "pipeline_index_size": len(stage_index),
        "pipeline_stages_read": stages_read,
        "stage_conflicts": stage_conflicts,
        "pipeline_read_failed": pipeline_read_failed,
        "goal_read_failed": goal_read_failed,
        # A zero with `degraded: true` is NOT a clean queue — it is a partial
        # scan. Never read one as the other (guard-1760).
        "degraded": degraded,
        "now": now.isoformat(timespec="seconds"),
    }

    if args.output == "human":
        print("scanned=%d with_hypothesis=%d terminal=%d dangling=%d%s"
              % (result["scanned"], with_hypothesis,
                 len(buckets["hypothesis_terminal"]),
                 len(buckets["hypothesis_dangling"]),
                 "  [DEGRADED — partial scan]" if degraded else ""))
        # Print the bound on every run, including the clean one — a zero is
        # only as good as the population it was measured over (guard-2529).
        print("  scope: %s" % result["scan_bound"])
        if stage_conflicts:
            print("  %d hypothesis id(s) present in >1 pipeline stage: %s"
                  % (len(stage_conflicts), sorted(stage_conflicts)[:3]))
        for name in ("hypothesis_terminal", "hypothesis_dangling"):
            for e in buckets[name]:
                days = ("%.1fd" % e["days_since_outcome"]
                        if e["days_since_outcome"] is not None else "?d")
                print("  [%s] %s (%s, %s, %s) %s since outcome | action=%s"
                      % (name, e["goal_id"], e["source"], e["intended_agent"],
                         e["priority"], days, e["action"]))
                print("      hyp=%s stage=%s outcome=%s reflected=%s resolved_by=%s"
                      % (e["hypothesis_id"], e["hypothesis_stage"],
                         e["outcome"], e["reflected"], e["resolved_by"]))
                if e["stage_conflict"]:
                    print("      STAGE CONFLICT: %s appears in %s — verdict used "
                          "the terminal one; confirm before acting"
                          % (e["hypothesis_id"], e["stage_conflict"]))
                if e["claimed_by"]:
                    print("      CLAIMED BY %s %s— it is theirs to close, not "
                          "yours (guard-1007)"
                          % (e["claimed_by"],
                             "(%.1fh ago) " % e["claim_age_hours"]
                             if e["claim_age_hours"] is not None else ""))
                if e["residual_scope_suspected"]:
                    why = "%d verification outcomes" % e["verification_outcome_count"]
                    if e["residual_markers"]:
                        why += "; prose markers %s" % (e["residual_markers"],)
                    print("      RESIDUAL SCOPE SUSPECTED (%s) — read the goal "
                          "before closing; the hypothesis may settle only part "
                          "of it" % why)
                print("      %s" % e["title"])
        if degraded:
            print("  DEGRADED: pipeline_read_failed=%s goal_read_failed=%s "
                  "— counts above are a FLOOR, not a measurement"
                  % (pipeline_read_failed, goal_read_failed))
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
