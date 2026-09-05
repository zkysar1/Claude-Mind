"""close_phase_skip — the DECISION half of the entry-time close-phase skip check.

Pure: no I/O, no subprocess, no clock. `decide()` takes an already-gathered
population plus two oracles and returns the report. The CLI half
(close-phase-skip-check.py) owns every read. Split so the decision is testable
without a live WM, a live store, or a live session (g-115-8219).

WHAT THIS DETECTS, and why nothing else could
---------------------------------------------
`iteration-close.sh --phase state-update` runs `loop-state-bump-counters.py`,
which appends the goal id to `loop_state.counted_goals_this_session`. That is
the ONLY writer of that list (verified: recurring-loop-state-mutate.py, the one
other loop_state writer, touches `goals_completed_this_session` and never the
counted list). So a goal this session closed which is NOT in the list is a goal
whose state-update did not run -- the counter bump, the journal append, the
iteration commit and the tree-drift reset all silently did not happen.

THE STRUCTURAL POINT (measured 2026-08-29, zeta/cc-02, during g-326-447 -- the
goal being CLOSED at the time, i.e. the incident victim, not an analysis of
this shape; see the role note at the runtime message below): an
autocompact resume dropped the loop back in at the CLOSE sequence, so
`--phase learning-gate` and `--phase productivity-check` ran while `--phase
verify` and `--phase state-update` never did. Every visible signal read healthy
-- productivity 0.90, encoding_ratio 1.00, the normal ITERATION COMPLETE banner
-- because the phases that would have left a sentinel ARE the phases that did
not run. Its absence is silent BY CONSTRUCTION.

The membership predicate is NOT new here: `loop-state-bump-counters.py
--verify-counted <goal-id>` has existed since g-115-1470, with careful
three-state semantics and a bounded torn-read retry. What was missing is a
caller at loop ENTRY. Its only caller was iteration-close.sh's own
do_state_update -- i.e. THE CALLER IS THE PHASE THAT DID NOT RUN, which is the
same defect one level down. This module supplies the missing caller and
reimplements none of the predicate (guard-2676, no-transcription).

THE AMBIGUITY, AND THE EXISTING SIGNAL THAT RESOLVES IT
-------------------------------------------------------
"Uncounted" has two causes and guard-1641 forbids reporting them as one: the
phase was SKIPPED, or the phase ran and its bump silently no-op'd (the bump is
fail-open at every layer and cannot report its own failure through rc).

The second cause already leaves a durable record. iteration-close.sh:2110 runs
the same `--verify-counted` predicate immediately after each bump and, on a
confident absence, appends a `bump_noop_detected` row to
`agents/<agent>/session/loop-state-bump-failures.jsonl` before re-firing once.
So the ledger IS the discriminator, and `decide()` takes it:

  uncounted + a ledger row  -> the bump no-op'd (and its re-fire also missed).
                               state-update DID run. Attribute to the bump.
  uncounted + no ledger row -> state-update never ran for this goal. THE FINDING.

That ledger had ONE writer and ZERO readers (grepped: only iteration-close.sh
writes it; the two other mentions are an experience note and an unrelated
example citation). Under learning-philosophy.md's detection-outranks-attribution
directive an unconsumed DETECTOR is the worse defect -- "the write cost is paid
AND the fleet still would not know asap if there is something wrong" -- so
wiring it here is the sanctioned disposition, not retiring it.

WHY NOT THE OBVIOUS SIGNAL -- and this was measured, not assumed
---------------------------------------------------------------
The tempting detector is "a completed goal missing `outcome_class`", which
iteration-close.sh:511 itself calls "the fingerprint" of verify's bookkeeping
not landing, and which IS present on the incident goal (g-326-447 carries
completed_date with outcome_class and completed_by_role both absent).

It is CONFOUNDED and must not be used as an alarm. Measured on the live store
2026-08-29: 127 of 623 completed goals lack `outcome_class`, 93 of them in the
preceding 9 days, across all five agents. g-115-6440 (pending, filed 2026-08-16)
had already measured the same population (122 of 597) and found the cause:
`aspirations-complete-by.sh` -- the DIRECT close path -- takes no
`--outcome-class` argument and never stamps it. So absent `outcome_class`
conflates "verify was skipped" with "closed through a path that never stamps
it", and an alarm on it would emit ~93 mostly-explained findings and duplicate
an open goal.

CONSEQUENCE WORTH CARRYING: that signal becomes clean the day g-115-6440 lands.
Once every close path stamps `outcome_class`, its absence would mean only "verify
did not run", and a second lane keyed on it would detect the
verify-skipped-but-state-update-ran shape this module cannot see. That is a
dependency, not a gap to route around.

REDUCER-SCOPED BY CONSTRUCTION
------------------------------
A worker Body never runs state-update at all (reducer-only-by-design in
worker_execute.LIFECYCLE_DISPOSITIONS), so EVERY worker close is uncounted and
an unscoped sweep would fire on all of them. Verified live: g-115-5819, closed
by an alpha worker Body, returns rc=1 "confidently uncounted" from the shipped
predicate. `decide()` therefore returns applicable=False on a worker rather than
a clean verdict -- "not my question" and "nothing wrong" must not render
identically (guard-1922: a check whose substrate is unreadable retires itself
silently, always as a pass).

RECURRING GOALS ARE IN SCOPE -- measured, against the obvious guess
------------------------------------------------------------------
It looks like recurring closes should be excluded, since they run through
recurring-close.sh and get their signal mutation from recurring-loop-state-
mutate.py. They are NOT excluded, on two measurements: recurring-close.sh:389
runs `iteration-close.sh --phase state-update`, which reaches the bump; and the
counted-list append at loop-state-bump-counters.py:486 is unconditional on
--recurring (only the Block A/B/D streak mutation is gated). Excluding them
would have blinded the check to every recurring close for a reason that reads
plausible and is false.
"""

from __future__ import annotations

# The states a per-goal answer can take. Only SKIPPED is a finding.
COUNTED = "counted"            # healthy: state-update ran and the bump landed
SKIPPED = "skipped"            # uncounted, no ledger row -> the phase did not run
BUMP_NOOP = "bump-noop"        # uncounted, ledger row -> the phase ran, bump missed
INDETERMINATE = "indeterminate"  # oracle could not tell (torn WM read)


def classify(goal_id, membership, bump_failures):
    """Classify ONE goal. Split out so the two-cause discrimination is directly
    testable without assembling a population."""
    verdict = membership(goal_id)
    if verdict == COUNTED:
        return COUNTED
    if verdict == INDETERMINATE:
        return INDETERMINATE
    # Confidently uncounted. Which cause?
    return BUMP_NOOP if goal_id in (bump_failures or ()) else SKIPPED


def decide(closed_goals, membership, bump_failures=(), *, role="reducer"):
    """Return the skip report.

    closed_goals:  list of dicts, each needing at least `id`. The caller has
        already scoped these to "closed by THIS agent in THIS session" --
        counted_goals_this_session is session-scoped, so a wider population
        would be compared against a list that never claimed to contain it.
    membership:    callable(goal_id) -> COUNTED | SKIPPED-ish | INDETERMINATE.
        In production this wraps `loop-state-bump-counters.py --verify-counted`,
        whose rc 0 means "counted OR indeterminate" and rc 1 means "confidently
        absent". Note the wrapper cannot distinguish counted from indeterminate
        -- the predicate deliberately collapses them to the conservative answer
        -- so INDETERMINATE arrives here only from a caller with a richer read.
    bump_failures: iterable of goal_ids appearing in loop-state-bump-failures.jsonl.
        Empty is the normal case and is NOT an assumption of health: it means no
        bump has been observed to no-op, so an uncounted goal has no competing
        explanation.
    role:          "reducer" | "worker". A worker is not applicable. Anything
        unrecognised is treated as a reducer -- failing toward LOOKING is the
        safe direction for a detector.
    """
    if role == "worker":
        return {
            "applicable": False,
            "reason": "worker Body — state-update is reducer-only-by-design, so "
                      "every worker close is uncounted and this check would fire "
                      "on all of them",
            "status": "clean",
            "completeness": "complete",
            "population": 0,
            "skipped": [],
            "bump_noop": [],
            "indeterminate": [],
        }

    skipped, bump_noop, indeterminate = [], [], []
    seen = 0
    for g in closed_goals:
        gid = (g or {}).get("id")
        if not gid:
            continue
        seen += 1
        verdict = classify(gid, membership, bump_failures)
        if verdict == SKIPPED:
            skipped.append(gid)
        elif verdict == BUMP_NOOP:
            bump_noop.append(gid)
        elif verdict == INDETERMINATE:
            indeterminate.append(gid)

    return {
        "applicable": True,
        "reason": None,
        # status answers "did I find anything"; completeness answers "did I see
        # everything". ORTHOGONAL, never collapsed -- the same contract
        # precheck-always-run-battery.py keeps, for the same reason: a zero with
        # any blind lane is UNREACHABLE, not EMPTY.
        #
        # bump_noop is deliberately NOT a finding: it is a known, self-healing
        # condition that already recorded itself and already re-fired. Counting
        # it here would re-alarm on something handled, and the population it
        # would add is exactly the population the ledger exists to own.
        "status": "findings" if skipped else "clean",
        "completeness": "partial" if indeterminate else "complete",
        "population": seen,
        "skipped": skipped,
        "bump_noop": bump_noop,
        "indeterminate": indeterminate,
    }


def render(report):
    """One human line. Never claims a cause it cannot separate."""
    if not report.get("applicable"):
        return f"close-phase-skip: n/a — {report.get('reason')}"
    pop = report.get("population", 0)
    skipped = report.get("skipped") or []
    noop = report.get("bump_noop") or []
    ind = report.get("indeterminate") or []

    if skipped:
        base = (
            f"close-phase-skip: {len(skipped)} of {pop} close(s) this session had "
            f"NO state-update — {', '.join(skipped)}. The counter bump, journal "
            "append, iteration commit and tree-drift reset did not happen for "
            "these. ESTABLISH THE CAUSE PER GOAL — do not inherit one. The shape "
            "this check was built from is an autocompact resume that re-entered "
            "the loop at the close sequence; g-326-447 is the goal that was BEING "
            "CLOSED when that happened (the incident VICTIM, evidence not "
            "analysis — its title is about a units guard and explains nothing). "
            "A batch close through a direct path leaves the IDENTICAL fingerprint "
            "(absent outcome_class + completed_by_role), so the fingerprint alone "
            "cannot tell the two apart. THIRD CAUSE, measured 2026-09-03 "
            "(bravo, cc-05, 6.8.0-138-generic) on g-326-802: a bare "
            "`aspirations-update-goal.sh <id> status completed` on an "
            "already-released, unclaimed goal. No close path runs at all, yet the "
            "status write still stamps completed_at/completed_by, so it lands the "
            "SAME fingerprint as both shapes above while being neither — it is not "
            "a resume (there was no close sequence) and not a batch (it was one "
            "goal). Distinct from guard-2660, which is the INVERSE (a REFUSED "
            "close: siblings land, status does not). "
            "THE DISCRIMINATOR IS THE CHANGELOG, NOT THE RECORD — one grep "
            "separates all three: `changelog-read.sh --limit 20000 | grep <goal-id>`. "
            "A bare-status close shows a lone `update-goal <id> status` (on "
            "g-326-802, with `update-goal <id> outcome_class` hand-backfilled 3h07m "
            "later); a real close shows `complete-by <id>`; an interrupted resume "
            "shows the close sequence's sibling writes without it. "
            "BUT `complete-by` PRESENT DOES NOT RULE OUT A RESUME — it locates "
            "WHERE the interruption landed, it does not exclude one. Measured "
            "2026-09-03 (alpha, cc-04, 6.8.0-138-generic) on g-115-4138: "
            "complete-by at 14:26:22 preceded by its normal siblings "
            "(progress_note, priority, outcome_note), then a LONE `update-goal "
            "<id> outcome_class` at 14:41:50 with no state-update writes between "
            "— a resume that re-entered AFTER the close write rather than before "
            "it. Reading only for the presence of `complete-by` scores that case "
            "'a real close' and stops, which is the misattribution this sentence "
            "used to invite. Read the SEQUENCE and the gaps in it, never the "
            "presence of any single row. Do that grep "
            "before attributing any cause here."
        )
    else:
        base = f"close-phase-skip: clean — {pop} close(s) this session, all counted"

    if noop:
        base += (f" ({len(noop)} uncounted but ledger-attributed to a bump no-op, "
                 f"not a skipped phase: {', '.join(noop)})")
    if ind:
        base += (f" INCOMPLETE: {len(ind)} goal(s) indeterminate (WM unreadable) — "
                 f"not evidence of health: {', '.join(ind)}")
    return base
