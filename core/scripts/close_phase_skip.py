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
mutate.py. They are NOT excluded, on two measurements -- both RE-DERIVED BY
SYMBOL 2026-09-07 (g-115-4366) after their line citations drifted. GREP THE
SYMBOLS, NOT THE NUMBERS; :389 and :486 now land on unrelated lines, and the
conclusion below survives the correction unchanged (guard-3503):

  1. recurring-close.sh's `run_phase state-update` line (cited :389, now ~:557)
     runs `iteration-close.sh --phase state-update`, which reaches the bump.
  2. the counted-list append in loop-state-bump-counters.py -- the pair
     `counted.append(args.goal_id)` / `loop_state["counted_goals_this_session"]
     = counted` (cited :486, now ~:628) -- is guarded ONLY by
     `if args.goal_id:`, and do_state_update passes `--goal-id "$GOAL_ID"`
     unconditionally. So the append is unconditional on --recurring; only the
     Block A/B/D streak mutation is gated, via iteration-close's `_rec_flag`.

Excluding them would have blinded the check to every recurring close for a
reason that reads plausible and is false.
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


# ── Close-PATH classification () ────────────────────────────────────
# `classify()` above answers WHY a goal is uncounted (phase skipped vs bump
# no-op). This answers a different question the check could not previously ask:
# was the close SANCTIONED — i.e. taken by a path that legitimately runs no
# iteration — and if so, should the missing state-update be EXCLUDED or CREDITED?
#
# WHY A CLASSIFIER RATHER THAN ANOTHER CAVEAT PARAGRAPH (guard-4649). Before
# this, every newly-discovered sanctioned path was answered by appending a
# numbered cause to render()'s text. That string reached FIVE causes and ~4,000
# characters, two of which end by telling the reader to confirm and open
# nothing — a detector instructing its reader to ignore it. A caveat in a
# detector's output is not a filter on the output: the firings continued at
# full rate and every reader paid the discrimination cost by hand (measured:
# one iteration spent an 11.1 MB changelog grep, two diary reads and three
# record reads re-deriving a cause that had already been established 7h
# earlier).
#
# WHY THE CHANGELOG AND NOT THE RECORD. Four record-derived discriminators were
# measured to failure across three boxes (key_finding present: 2 of 10, and one
# of those was NOT a drain; no live claim at close: claimed_by None on ALL TEN
# including both loss candidates; completed_by != executed_by: does not
# separate; `Maintain:` title prefix: 40 of 83 completed Maintain goals carry
# outcome_class, so excluding them would suppress ~40 genuine losses to save 1
# false alarm). They fail for one structural reason: every one of them describes
# what the goal WAS ABOUT or WHO touched it, while the sanctioned-vs-lost split
# is a fact about HOW THE GOAL WAS CLOSED — which lane called which script. Only
# the changelog records that.
#
# THE SEPARATOR IS QUANTITATIVE. A sanctioned non-executing close writes
# `complete-by <id>` and then `update-goal <id> outcome_class` SECONDS apart
# with no other row naming that goal between them. Measured gaps: 2s, 4s, 4s,
# 5s, 6s, 10s, 10s, 22s (n=8, four boxes). The shape it must not be confused
# with — an autocompact resume that re-entered after the close write — puts its
# lone outcome_class row 15m28s later (). Two orders of magnitude
# apart. The threshold below sits 2.7x above the largest observed sanctioned gap
# and ~15x below the observed resume gap.
SANCTIONED_GAP_SECONDS = 60

PATH_SANCTIONED_DISPOSAL = "sanctioned-disposal"  # closer executed nothing -> EXCLUDE
PATH_SANCTIONED_SELF = "sanctioned-self"          # closer DID work -> CREDIT (real gap)
PATH_UNCLASSIFIED = "unclassified"                # stays a finding


def _ts_seconds(stamp):
    """Naive `YYYY-MM-DDTHH:MM:SS` -> epoch-ish seconds. Returns None if
    unparseable — an unreadable stamp must never render as a small gap."""
    try:
        from datetime import datetime
        return datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S").timestamp()
    except (ValueError, TypeError):
        return None


def classify_close_path(rows, closing_agent=None):
    """Classify HOW one goal was closed, from that goal's changelog rows.

    rows: chronological list of dicts for ONE goal id, each {ts, agent, op,
        field}. `op` is one of claim | release | complete-by | update-goal |
        add-goal; `field` is set only for update-goal.
    closing_agent: the agent whose session is being checked.

    Returns one of the PATH_* constants. Every ambiguous input returns
    PATH_UNCLASSIFIED, because under-matching is the safe direction here: a
    wrongly-suppressed real loss is invisible, while one extra reported row
    costs a reader thirty seconds (this goal's own stated criterion).
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return PATH_UNCLASSIFIED

    complete_by = next((r for r in rows if r.get("op") == "complete-by"), None)
    if complete_by is None:
        # No complete-by at all: a bare-status close or a resume. Not sanctioned
        # by anything this function can see.
        return PATH_UNCLASSIFIED

    t0 = _ts_seconds(complete_by.get("ts"))
    oc = next((r for r in rows
               if r.get("op") == "update-goal" and r.get("field") == "outcome_class"
               and (_ts_seconds(r.get("ts")) or -1) >= (t0 or 0)), None)
    if oc is None or t0 is None:
        return PATH_UNCLASSIFIED
    t1 = _ts_seconds(oc.get("ts"))
    if t1 is None or (t1 - t0) > SANCTIONED_GAP_SECONDS:
        return PATH_UNCLASSIFIED

    # Nothing else naming this goal may sit between the pair. Rows for other
    # files (team-state, presence) never name the goal, so they are already
    # absent from `rows` and correctly do not break the fingerprint.
    for r in rows:
        t = _ts_seconds(r.get("ts"))
        if t is None or r is complete_by or r is oc:
            continue
        if t0 < t < t1:
            return PATH_UNCLASSIFIED

    # Sanctioned fingerprint confirmed. EXCLUDE vs CREDIT now turns on whether
    # the CLOSING agent did the work, which is what the claim history records.
    claims = [r for r in rows if r.get("op") == "claim"]
    if not claims:
        # Never claimed by anyone, yet closed through a real close path: the
        # closer did the work without a claim cycle (the measured instance is a
        # hypothesis-resolution close, whose learning landed in the pipeline
        # record). Work HAPPENED, so the absent counter bump, journal append and
        # tree-drift reset are a real accounting gap -> CREDIT.
        return PATH_SANCTIONED_SELF
    if closing_agent and all(r.get("agent") and r.get("agent") != closing_agent
                             for r in claims):
        # Every claim belongs to somebody else: this is a disposal of another
        # Body's finished-but-unbanked unit. The closer executed nothing, so
        # crediting it would book work that did not happen here -> EXCLUDE.
        return PATH_SANCTIONED_DISPOSAL
    # Claimed by the closing agent at some point. The fingerprint says the close
    # ran no iteration, but a same-agent claim cannot separate "disposed my own
    # stale row" from "executed and lost the close". Leave it to a reader.
    return PATH_UNCLASSIFIED


def decide(closed_goals, membership, bump_failures=(), *, role="reducer",
           close_path=None):
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
    sanctioned_excluded, sanctioned_credited = [], []
    seen = 0
    for g in closed_goals:
        gid = (g or {}).get("id")
        if not gid:
            continue
        seen += 1
        verdict = classify(gid, membership, bump_failures)
        if verdict == SKIPPED:
            # An uncounted close is a FINDING only if it was not taken by a path
            # that legitimately runs no iteration. Absent oracle => every
            # uncounted close stays a finding, so behaviour without a changelog
            # read is byte-identical to before this split ().
            path = close_path(gid) if close_path else PATH_UNCLASSIFIED
            if path == PATH_SANCTIONED_DISPOSAL:
                sanctioned_excluded.append(gid)
                continue
            if path == PATH_SANCTIONED_SELF:
                sanctioned_credited.append(gid)
                continue
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
        "status": "findings" if (skipped or sanctioned_credited) else "clean",
        "completeness": "partial" if indeterminate else "complete",
        "population": seen,
        "skipped": skipped,
        # EXCLUDE: the closer executed nothing (disposal of another Body's
        # unbanked unit), so a counter bump would book work that did not happen.
        "sanctioned_excluded": sanctioned_excluded,
        # CREDIT: the closer DID work through a path that runs no iteration, so
        # the absent bump / journal append / tree-drift reset is a real
        # accounting gap. Reported separately, never silently suppressed.
        "sanctioned_credited": sanctioned_credited,
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
            "these. These are the closes the classifier could NOT attribute to a "
            "sanctioned non-executing path, so establish the cause per goal: "
            "`changelog-read.sh --limit 20000 | grep <goal-id>` and read the "
            "SEQUENCE and its gaps, never the presence of any single row. The six "
            "measured close shapes and which are sanctioned: "
            "core/config/rationale/close-phase-skip-causes.md."
        )
    else:
        base = f"close-phase-skip: clean — {pop} close(s) this session, all counted"

    if noop:
        base += (f" ({len(noop)} uncounted but ledger-attributed to a bump no-op, "
                 f"not a skipped phase: {', '.join(noop)})")
    excl = report.get("sanctioned_excluded") or []
    cred = report.get("sanctioned_credited") or []
    if excl:
        base += (f" [{len(excl)} EXCLUDED: closed via a sanctioned path that "
                 f"executed no iteration, so no counter was owed: "
                 f"{', '.join(excl)}]")
    if cred:
        base += (f" [{len(cred)} ACCOUNTING GAP: sanctioned close path, but work "
                 f"WAS done — the bump/journal/tree-drift reset is genuinely "
                 f"owed and did not happen: {', '.join(cred)}]")
    if ind:
        base += (f" INCOMPLETE: {len(ind)} goal(s) indeterminate (WM unreadable) — "
                 f"not evidence of health: {', '.join(ind)}")
    return base
