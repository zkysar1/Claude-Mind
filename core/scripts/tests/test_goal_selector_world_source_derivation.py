"""test_goal_selector_world_source_derivation.py --  regression.

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
  4. world goal, intended_agent == other LIVE agent -> DROPPED (routing filter)
  5. world goal, intended_agent == an OFF-ROSTER name -> KEPT (g-115-3482:
     a retired agent or unrecognized sentinel names nobody who can honor the
     routing, so filtering it made the goal invisible in BOTH the candidate
     and blocked lists -- 2 goals unreachable for 71 days)
  6. source is NEVER "agent" nor "cross-agent:*" for any world goal
     (the explicit negative assertion the incident motivates)

Invariants 4 and 5 are complements, and keeping them apart is why OTHER_AGENT
is now derived from the live vocabulary rather than hardcoded -- see the
comment at its definition.

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

# OTHER_AGENT must name a LIVE peer. It was hardcoded `"delta"` until
# 2026-07-28 -- but delta was RETIRED on 2026-07-07, and from that day the
# fixture silently stopped testing "routed to ANOTHER AGENT" and started
# testing "routed to a NONEXISTENT agent". Those became two different cases in
# : an off-roster target names nobody who can honor the routing, so
# the goal now falls THROUGH (staying visible) instead of vanishing from both
# the candidate list and the blocked list. The fixture is itself an instance of
# the bug class it tripped over -- a real name that rotted into a synthetic one.
# Derive from the live vocabulary so a future retirement cannot repeat it.
from aspirations import _valid_intended_agents as _vocab  # noqa: E402

_LIVE_PEERS = sorted(n for n in _vocab() if n not in (SELF_AGENT, "either"))
# Fallback keeps the case meaningful where the roster is unresolvable: the
# vocabulary check is SKIPPED in that state, so a plain name-mismatch still
# routes away and the invariant still holds.
OTHER_AGENT = _LIVE_PEERS[0] if _LIVE_PEERS else "alpha"

# A name that is NOT on the roster -- the  case, pinned explicitly
# below so both halves of the routing contract live in this file.
OFFROSTER_AGENT = "delta"


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


def case_offroster_routed_world_goal_kept() -> tuple[bool, str]:
    """world goal, intended_agent == an OFF-ROSTER name -> KEPT ().

    The complement of the case above, and the reason that one had to stop
    hardcoding a name. A value outside the vocabulary (a retired agent, or an
    unrecognized sentinel like the cycle-detector's "any") names nobody who can
    ever honor the routing. Filtering it made the goal invisible in BOTH
    directions -- absent from the candidate list, and never classified by
    collect_blocked, which did not reference intended_agent at all. Two goals
    sat unreachable for 71 days that way. Falling through restores them to
    exactly the visibility "either" would give.

    UPDATED g-115-3679: collect_blocked now DOES carry an intended_agent
    inverse (block_reason "routed_to_agent"), so the both-directions vanish no
    longer applies to an ON-ROSTER target. This case is unaffected -- an
    off-roster name still falls THROUGH to visible rather than being blocked,
    and test_goal_selector_intended_agent_inverse.py pins that boundary from
    the other side.

    Skipped when the roster is unresolvable: the vocabulary check is disabled
    in that state by design (an unreadable team-state must not make every
    routed goal fleet-visible), so the case would not be meaningful.
    """
    if not _LIVE_PEERS:
        return True, "skipped (roster unresolvable -- vocabulary check disabled by design)"
    results = _collect_world([_make_goal("g-offroster",
                                         intended_agent=OFFROSTER_AGENT)])
    ids = [c["goal"]["id"] for c in results]
    if ids != ["g-offroster"]:
        return False, (f"expected ['g-offroster'] (off-roster target falls "
                       f"through to visible), got {ids}")
    if results[0].get("source") != "world":
        return False, f"source mutated: {results[0].get('source')!r}"
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
    ("offroster-routed-world-goal-kept",           case_offroster_routed_world_goal_kept),
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
