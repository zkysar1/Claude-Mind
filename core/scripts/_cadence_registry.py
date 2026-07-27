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
  IN  — the six cadences whose fire-action is a single LLM SKILL INVOCATION:
        fresh-eyes-review (0.5e), fresh-eyes-program (0.5e.5),
        fresh-eyes-tree (0.5e.7), felt-sense (0.5f), curriculum (0.5i),
        evolution (0.5j).
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
  meter_name     the aspirations-precheck-budget-meter.sh sweep name. All six are
                 `deferrable` tier — the meter drops them ONLY in the tight zone.
                 The battery does NOT read the meter (the checks are cheap +
                 read-only); the SKILL.md dispatch loop meter-gates the expensive
                 SKILL INVOCATION on FIRE, carrying this name.
  fire_dispatch  human dispatch string the battery prints on FIRE — names the
                 skill the SKILL.md dispatch loop must invoke.
"""

from __future__ import annotations

CADENCES: list[dict] = [
    {
        "name": "fresh-eyes-review",
        "phase": "0.5e",
        "check_cmd": ["fresh-eyes-cadence-check.sh"],
        "meter_name": "fresh-eyes-cadence",
        "fire_dispatch": "invoke /fresh-eyes-review --cadence",
    },
    {
        "name": "fresh-eyes-program",
        "phase": "0.5e.5",
        "check_cmd": ["fresh-eyes-cadence-check.sh", "--config-block", "fresh_eyes_program"],
        "meter_name": "fresh-eyes-program-cadence",
        "fire_dispatch": "invoke /fresh-eyes-program --cadence",
    },
    {
        "name": "fresh-eyes-tree",
        "phase": "0.5e.7",
        "check_cmd": ["fresh-eyes-cadence-check.sh", "--config-block", "fresh_eyes_tree"],
        "meter_name": "fresh-eyes-tree-cadence",
        "fire_dispatch": "invoke /fresh-eyes-tree --cadence",
    },
    {
        "name": "felt-sense",
        "phase": "0.5f",
        "check_cmd": ["felt-sense-cadence-check.sh"],
        "meter_name": "felt-sense-cadence",
        "fire_dispatch": "invoke /felt-sense-checkin --cadence",
    },
    {
        "name": "curriculum",
        "phase": "0.5i",
        "check_cmd": ["curriculum-cadence-check.sh"],
        "meter_name": "curriculum-cadence",
        "fire_dispatch": "invoke /curriculum-gates (re-evaluate gates; route to promotion if all pass)",
    },
    {
        "name": "evolution",
        "phase": "0.5j",
        "check_cmd": ["evolution-cadence-check.sh"],
        "meter_name": "evolution-cadence",
        "fire_dispatch": "invoke /aspirations-evolve",
    },
]


def cadences() -> list[dict]:
    """All registered skill-invocation cadence gates, in precheck phase order."""
    return list(CADENCES)


def cadence_names() -> list[str]:
    """Registered cadence display names, order preserved."""
    return [c["name"] for c in CADENCES]
