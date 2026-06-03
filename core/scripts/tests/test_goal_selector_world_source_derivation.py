"""test_goal_selector_world_source_derivation.py -- 5 regression.

Pins the source-derivation contract that the g-115-980 incident
(2026-05-21T12:58:05) questioned: does goal-selector.py compute `source`
correctly for a WORLD goal tagged with `intended_agent` set to a single
agent? The incident observed source=agent on g-115-980's
iteration-checkpoint.json even though g-115-980 lives in the world queue
(asp-115, intended_agent=alpha).

The g-115-1065 investigation read collect_candidates at both the current
commit and the pre-incident commit (ec735533, 2026-05-19) and found them
structurally identical for source assignment:

  - source is tagged from the `source` ARG (the queue file read), NEVER
    derived from `intended_agent` (goal-selector.py L859, L1089).
  - `intended_agent` only FILTERS (drop-if-routed-to-another-agent,
    L1083-1087); it never mutates the source field.
  - collect_cross_agent_candidates (the only producer of "cross-agent:*"
    sources) scans ONLY agents/*/aspirations.jsonl, never the world queue.

So a world goal CANNOT receive source=agent or source=cross-agent:* from
the selector. The incident's source=agent therefore came from a
checkpoint-lifecycle path (stale carryover / post-compact restore), not
from source derivation -- the selector is exonerated.

The sibling test test_goal_selector_cross_agent_pull.py (g-115-946) pins
collect_cross_agent_candidates. This file pins the complementary half:
collect_candidates(source="world") keeps source="world" regardless of the
goal's intended_agent value. Together they fully cover the source-field
contract the incident questioned.

Tested invariants:
  1. world goal, intended_agent == AGENT_NAME -> kept, source == "world"
  2. world goal, intended_agent == "either"   -> kept, source == "world"
  3. world goal, intended_agent == None        -> kept, source == "world"
  4. world goal, intended_agent == other-agent -> DROPPED (routing filter)
  5. source is NEVER "agent" nor "cross-agent:*" for any world goal
     (the explicit negative assertion the incident motivates)

Pattern: build synthetic world aspirations, call collect_candidates
directly with source="world", assert the source tag on each result.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# goal-selector.py requires MIND_AGENT to load (paths derive AGENT_DIR) and
# uses module-level AGENT_NAME for the intended_agent routing filter inside
# collect_candidates. Capture-restore around the module-level mutation so
# env pollution cannot leak to other tests (Layer 1 test-pollution defense --
# rb-1096, guard-588). collect_candidates reads AGENT_NAME at call time, so
# binding it here ("bravo") fixes the "self" identity for these cases.
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "bravo")

gs = importlib.import_module("goal-selector")
collect_candidates = gs.collect_candidates

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT

# collect_candidates uses module-level AGENT_NAME for the intended_agent
# filter; tests align "self" to that value.
SELF_AGENT = gs.AGENT_NAME            # "bravo" per the setdefault above
OTHER_AGENT = "delta" if SELF_AGENT != "delta" else "alpha"


def _make_goal(goal_id: str, intended_agent: str | None,
               status: str = "pending") -> dict:
    """Minimal goal record passing all eligibility filters (mirrors the
    helper in test_goal_selector_cross_agent_pull.py)."""
    g = {
        "id": goal_id,
        "title": f"test goal {goal_id}",
        "status": status,
        "priority": "MEDIUM",
        "category": "framework-architecture",
        "participants": ["agent"],
        "recurring": False,
    }
    if intended_agent is not None:
        g["intended_agent"] = intended_agent
    return g


def _make_aspiration(asp_id: str, goals: list[dict]) -> dict:
    return {
        "id": asp_id,
        "title": f"test aspiration {asp_id}",
        "status": "active",
        "goals": goals,
        "priority": "MEDIUM",
    }


def _collect_world(goals: list[dict]) -> list[dict]:
    """Run collect_candidates over a single active world aspiration."""
    asps = [_make_aspiration("asp-world-test", goals)]
    return collect_candidates(asps, source="world")


def case_self_routed_world_goal_keeps_world_source() -> tuple[bool, str]:
    """world goal, intended_agent == SELF -> kept, source == 'world'."""
    results = _collect_world([_make_goal("g-self", intended_agent=SELF_AGENT)])
    ids = [c["goal"]["id"] for c in results]
    if ids != ["g-self"]:
        return False, f"expected ['g-self'], got {ids}"
    src = results[0].get("source")
    if src != "world":
        return False, f"intended_agent leaked into source: got {src!r}, expected 'world'"
    return True, "ok"


def case_either_world_goal_keeps_world_source() -> tuple[bool, str]:
    """world goal, intended_agent == 'either' -> kept, source == 'world'."""
    results = _collect_world([_make_goal("g-either", intended_agent="either")])
    ids = [c["goal"]["id"] for c in results]
    if ids != ["g-either"]:
        return False, f"expected ['g-either'], got {ids}"
    if results[0].get("source") != "world":
        return False, f"wrong source: {results[0].get('source')!r}"
    return True, "ok"


def case_unset_world_goal_keeps_world_source() -> tuple[bool, str]:
    """world goal, intended_agent unset (None) -> kept, source == 'world'."""
    results = _collect_world([_make_goal("g-none", intended_agent=None)])
    ids = [c["goal"]["id"] for c in results]
    if ids != ["g-none"]:
        return False, f"expected ['g-none'], got {ids}"
    if results[0].get("source") != "world":
        return False, f"wrong source: {results[0].get('source')!r}"
    return True, "ok"


def case_other_routed_world_goal_dropped() -> tuple[bool, str]:
    """world goal, intended_agent == OTHER -> dropped by routing filter.

    This is the only intended_agent effect on collection: a drop. It is
    NOT a source mutation -- the goal simply does not appear.
    """
    results = _collect_world([_make_goal("g-other", intended_agent=OTHER_AGENT)])
    ids = [c["goal"]["id"] for c in results]
    if ids != []:
        return False, f"expected [] (routed away), got {ids}"
    return True, "ok"


def case_source_never_agent_or_cross_agent() -> tuple[bool, str]:
    """The incident's explicit negative: no world goal ever gets source in
    ('agent', 'cross-agent:*'). Mixed-intended_agent batch, assert every
    surviving candidate is source == 'world'."""
    goals = [
        _make_goal("g-w-self",   intended_agent=SELF_AGENT),
        _make_goal("g-w-either", intended_agent="either"),
        _make_goal("g-w-none",   intended_agent=None),
        _make_goal("g-w-other",  intended_agent=OTHER_AGENT),  # dropped
    ]
    results = _collect_world(goals)
    for c in results:
        src = c.get("source")
        if src != "world":
            return False, (f"{c['goal']['id']} got source={src!r}; world goals "
                           f"must be source='world' (g-115-1065 contract)")
        if src == "agent" or str(src).startswith("cross-agent:"):
            return False, f"{c['goal']['id']} source leaked to {src!r}"
    surviving = sorted(c["goal"]["id"] for c in results)
    expected = ["g-w-either", "g-w-none", "g-w-self"]
    if surviving != expected:
        return False, f"survivors {surviving} != expected {expected}"
    return True, "ok"


CASES = [
    ("self-routed-world-goal-keeps-world-source",  case_self_routed_world_goal_keeps_world_source),
    ("either-world-goal-keeps-world-source",       case_either_world_goal_keeps_world_source),
    ("unset-world-goal-keeps-world-source",        case_unset_world_goal_keeps_world_source),
    ("other-routed-world-goal-dropped",            case_other_routed_world_goal_dropped),
    ("source-never-agent-or-cross-agent",          case_source_never_agent_or_cross_agent),
]


def main() -> int:
    failures = []
    for cid, fn in CASES:
        try:
            ok, msg = fn()
        except Exception as e:
            failures.append(f"[FAIL] {cid}: raised {type(e).__name__}: {e}")
            continue
        if not ok:
            failures.append(f"[FAIL] {cid}: {msg}")
        else:
            print(f"[PASS] {cid}")

    if failures:
        for f in failures:
            print(f)
        print(f"\n{len(failures)} / {len(CASES)} FAILED")
        return 1
    print(f"\nAll {len(CASES)} passed.")
    return 0


# pytest entrypoint mirrors the cross-agent-pull test pattern
def test_world_source_derivation() -> None:
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
