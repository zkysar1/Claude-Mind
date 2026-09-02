"""_cadence_registry — single source of truth for the deferrable
skill-invocation precheck cadence gates (g-115-2984, fix for g-115-2982).

Shared by:
  - precheck-cadence-battery.py (g-115-2984): runs every registered cadence gate
    check in ONE call at aspirations-precheck Phase 0.5-cadence-battery, so a
    compaction summary need only preserve "run the cadence battery" instead of
    six separate LLM-orchestrated phase enumerations. The starvation class it
    kills: felt-sense (0.5f) starved 3 days / 581 goals because the phase was
    never invoked — the LLM abbreviated it under context pressure. The budget
    meter was NOT the cause (g-115-2982 refuted that: the wall-clock drop path
    was removed 2026-06-15 by g-115-1489, and zone is "fresh" ~814:1 so the
    tight-zone drop is structurally near-unreachable). A non-fire at diff
    581>>cadence 75 proved the gate was never run. This is the g-115-2303
    sentinel-battery pattern applied to cadences.

Adding a new skill-invocation cadence gate? Add ONE entry here (and rewire its
consumer phase in aspirations-precheck/SKILL.md to dispatch from the battery
output). The battery picks it up from this list — do not re-enumerate cadences
in the battery script.

SCOPE (principled, NOT a silent cap — the failure mode this fix targets is
"LLM skips the phase -> the cadence-check gate is never run -> the skill is
never invoked -> the ritual starves"):
  IN  — the seven cadences whose fire-action is a single LLM SKILL INVOCATION,
        in phase order (the order CADENCES below is kept in):
        fresh-eyes-review (0.5e), fresh-eyes-program (0.5e.5),
        fresh-eyes-tree (0.5e.7), strategic-scan (0.5e.9), felt-sense (0.5f),
        curriculum (0.5i), evolution (0.5j).
  IN, ADDED 2026-08-02 (g-115-4691) — strategic-scan. It was not excluded on
        shape grounds; it was NEVER CONSIDERED, and this docstring's silence is
        the only place that showed it. Its fire-action is one skill invocation,
        so it met the IN criterion from day one — it was simply invisible here
        because its consumer phase lives at ORCHESTRATOR Phase 1.5 rather than
        in precheck, and this list was assembled from precheck phases. It then
        starved exactly as predicted: 19.5h against a 4h cadence (alpha, cc-04,
        2026-08-02), with nothing in bash reading last_strategic_scan at all.
        A Phase-1.5-local gate was considered and rejected as structurally
        impossible — a bash call inside an LLM-skippable block inherits the
        skippability (measured: the phase-start/phase-end diary markers already
        sitting inside that block produced 0 markers in 178 diary lines on a box
        where the stamp was 3.9h fresh). Idempotent with Phase 1.5 via the shared
        last_strategic_scan stamp, the same pairing evolution uses with Phase 8.8.
        When adding a cadence, check the ORCHESTRATOR phases too — not only
        precheck's.
  OUT — l1-skew (0.5g): SELF-ACTING — it posts findings to the board INSIDE the
        script via --post-board, so its value does not depend on a follow-up LLM
        skill invocation; it cannot starve via the skill-skip mode. Keeps its
        own phase.
  OUT — health-regression (0.5h): DORMANT (collect-only launch default) with a
        multi-step verify -> verdict -> investigate-file -> tiered-revert flow
        that does not fit the uniform "gate -> exit 0 -> invoke one skill" shape.
        Keeps its own phase.
Both exclusions sit OUTSIDE the skill-invocation-skip starvation class; extending
the battery to them if ever warranted is a separate follow-up.

Entry fields:
  name           display name (battery output + registry helper keys)
  phase          the aspirations-precheck phase the cadence lives in
  check_cmd      argv list for the read-only cadence gate check; element 0 is the
                 script basename under core/scripts/. Exit 0 = FIRE (cadence
                 crossed), exit 1 (or any non-zero) = noop. The check scripts
                 "only read state" (goal-count vs last-fire), so running them in
                 the battery is side-effect-free.
  meter_name     the aspirations-precheck-budget-meter.sh sweep name. All are
                 `deferrable` tier — the meter drops them ONLY in the tight zone.
                 The battery does NOT read the meter (the checks are cheap +
                 read-only); the SKILL.md dispatch loop meter-gates the expensive
                 SKILL INVOCATION on FIRE, carrying this name.
  dispatch_skill the skill the FIRE action invokes, as its slash name
                 (e.g. "/fresh-eyes-review"). MACHINE-READABLE — this is the
                 field a consumer joins on. See "The dispatch stage" below.
  dispatch_args  argument string passed with the skill, "" when there are none.
  dispatch_note  free prose qualifying the invocation ("re-evaluate gates; ...")
                 or "" — the ONLY part of the dispatch that is addressed to a
                 reader rather than to a program.
  fire_dispatch  DERIVED human dispatch string the battery prints on FIRE.
                 Computed from the three fields above by _compose_dispatch();
                 it is NOT hand-written per entry. See below for why.

The dispatch stage (g-115-5396, guard-5298)
-------------------------------------------
`fire_dispatch` used to be a hand-written PROSE STRING per entry, and that is
the whole of the defect guard-5298 records: "a config field whose VALUE is an
instruction addressed to a reader (invoke /x, then run y) is an unexecuted
stage wearing a config schema. Grep for who CONSUMES it; if the only consumer
is a print statement, there is no automation there and no counter can be
measuring it." Measured 2026-08-27 (foxtrot, LAPTOP-3IOFCNEO, fresh-eyes N=80):
the fresh-eyes ritual was 42 goals past due while precheck-drops.jsonl read
sweeps_dropped=0 / zone=fresh / tail_reached=true on every record — every
counter TRUE, every one of them measuring whether the CHECK ran, none observing
the DISPATCH. Six sibling Investigate goals accumulated, one per cadence
(g-115-4913/4967/5396/5835/6167/6585), because nothing pointed at the common
seam.

WHAT THIS SPLIT DOES AND DOES NOT FIX. It does NOT make the dispatch execute:
every entry's fire-action is a Claude SKILL invocation, and no script can call
one — the LLM remains the sole executor, by construction and not by oversight.
What it fixes is that the dispatch is now JOINABLE: `dispatch_skill` is a
stable identifier a consumer can match, route on, or check for, where prose was
neither parseable nor uniform (compare "invoke /aspirations-evolve" against
"invoke /curriculum-gates (re-evaluate gates; route to promotion if all pass)").
`cadence-stale-canary.py` is the first non-print consumer: it files a goal
naming the exact `Skill(...)` call instead of an investigation into a question
that is already answered.

`fire_dispatch` is DERIVED rather than stored so the prose and the structured
form cannot drift — the single-source-of-truth rule in
`.claude/rules/communication-clarity.md` (5). The composition reproduces all
seven legacy strings byte-for-byte; `test_cadence_registry_dispatch.py` pins
that against the literals, so a future edit to the composer that changes any
printed line fails loudly rather than silently rewording an imperative.

ROUTING IS NOT UNIFORM, and the structured field is what makes that visible:
six of the seven skills are worker-eligible and `/aspirations-evolve` is
reducer-only-by-design (measured via `worker_execute.py skill-eligible`, which
is the SSOT — do NOT hardcode a second copy of that answer here).
"""

from __future__ import annotations


def _compose_dispatch(skill: str, args: str = "", note: str = "") -> str:
    """Render the human dispatch line from the structured dispatch fields.

    The ONE writer of `fire_dispatch`. Kept trivial on purpose: it exists to
    remove a second hand-maintained copy of the same fact, not to add behavior.
    """
    out = f"invoke {skill}"
    if args:
        out += f" {args}"
    if note:
        out += f" ({note})"
    return out


CADENCES: list[dict] = [
    {
        "name": "fresh-eyes-review",
        "phase": "0.5e",
        "check_cmd": ["fresh-eyes-cadence-check.sh"],
        "meter_name": "fresh-eyes-cadence",
        "dispatch_skill": "/fresh-eyes-review",
        "dispatch_args": "--cadence",
        "dispatch_note": "",
    },
    {
        "name": "fresh-eyes-program",
        "phase": "0.5e.5",
        "check_cmd": ["fresh-eyes-cadence-check.sh", "--config-block", "fresh_eyes_program"],
        "meter_name": "fresh-eyes-program-cadence",
        "dispatch_skill": "/fresh-eyes-program",
        "dispatch_args": "--cadence",
        "dispatch_note": "",
    },
    {
        "name": "fresh-eyes-tree",
        "phase": "0.5e.7",
        "check_cmd": ["fresh-eyes-cadence-check.sh", "--config-block", "fresh_eyes_tree"],
        "meter_name": "fresh-eyes-tree-cadence",
        "dispatch_skill": "/fresh-eyes-tree",
        "dispatch_args": "--cadence",
        "dispatch_note": "",
    },
    {
        # . Phase id 0.5e.9 places it after the fresh-eyes trio and
        # before felt-sense (0.5f); it has no precheck phase of its own — the
        # battery IS its dispatch point, with orchestrator Phase 1.5 retained as
        # the sooner-firing goal_cadence / recurring_settling path.
        "name": "strategic-scan",
        "phase": "0.5e.9",
        "check_cmd": ["strategic-scan-cadence-check.sh"],
        "meter_name": "strategic-scan-cadence",
        "dispatch_skill": "/aspirations-strategic-scan",
        "dispatch_args": "",
        "dispatch_note": "scan_trigger=time_cadence",
    },
    {
        "name": "felt-sense",
        "phase": "0.5f",
        "check_cmd": ["felt-sense-cadence-check.sh"],
        "meter_name": "felt-sense-cadence",
        "dispatch_skill": "/felt-sense-checkin",
        "dispatch_args": "--cadence",
        "dispatch_note": "",
    },
    {
        "name": "curriculum",
        "phase": "0.5i",
        "check_cmd": ["curriculum-cadence-check.sh"],
        "meter_name": "curriculum-cadence",
        "dispatch_skill": "/curriculum-gates",
        "dispatch_args": "",
        "dispatch_note": "re-evaluate gates; route to promotion if all pass",
    },
    {
        "name": "evolution",
        "phase": "0.5j",
        "check_cmd": ["evolution-cadence-check.sh"],
        "meter_name": "evolution-cadence",
        "dispatch_skill": "/aspirations-evolve",
        "dispatch_args": "",
        "dispatch_note": "",
    },
]

# Derive the printed prose ONCE, at import, for every entry. Consumers that read
# `fire_dispatch` (precheck-cadence-battery, cadence-stale-canary, the tests)
# keep working unchanged and cannot see a value that disagrees with the
# structured fields, because there is only one writer.
for _c in CADENCES:
    _c["fire_dispatch"] = _compose_dispatch(
        _c["dispatch_skill"], _c["dispatch_args"], _c["dispatch_note"]
    )
del _c


def cadences() -> list[dict]:
    """All registered skill-invocation cadence gates, in precheck phase order."""
    return list(CADENCES)


def cadence_names() -> list[str]:
    """Registered cadence display names, order preserved."""
    return [c["name"] for c in CADENCES]
