"""test_verification_outcomes_agent_leg.py --  (US-04).

verification.outcomes_agent_leg is the agent-side subset of success criteria
on a participants:[agent,user] collaborative goal, separate from outcomes
(which span both legs). It lets Phase 5 verify close "agent leg complete,
user leg pending" as a valid terminal state without inventing closure
justification (the US-04 anti-pattern: zeta hedged g-250-80 closure twice
because the schema forced "agent done, user pending" through a single field).

This pins the validator (core/scripts/aspirations.py validate_verification):
the field is OPTIONAL (absent -> no change, backward compatible) and when
present MUST be a list -- a scalar/dict/int raises a clean ValueError naming
the field, identical to the existing verification.outcomes contract. The
companion verify-handler branch lives in
.claude/skills/aspirations-verify/SKILL.md ("Collaborative Goal Agent-Leg
Terminal State").
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import aspirations  # noqa: E402


# --- validate_verification: the new field is optional + list-typed ---------

def test_absent_field_passes():
    """Backward compatibility: a verification without outcomes_agent_leg validates."""
    aspirations.validate_verification({"outcomes": ["x"]}, "g-000-00")


def test_none_field_passes():
    """Explicit null is allowed (optional field, same as outcomes)."""
    aspirations.validate_verification(
        {"outcomes": ["x"], "outcomes_agent_leg": None}, "g-000-00")


def test_list_field_passes():
    """A list of agent-leg outcomes validates."""
    aspirations.validate_verification(
        {"outcomes_agent_leg": ["agent wired the schema", "tests pass"]},
        "g-000-00")


def test_empty_list_passes():
    """An empty list is still a list -- the verify-handler branch keys on
    NON-empty to fire, so the validator only enforces list-ness here."""
    aspirations.validate_verification({"outcomes_agent_leg": []}, "g-000-00")


@pytest.mark.parametrize("bad", ["a string", {"k": "v"}, 42, ("a", "b")])
def test_non_list_raises_valueerror(bad):
    """A scalar/dict/int/tuple where a list is expected raises a clean
    ValueError naming the field -- identical contract to verification.outcomes."""
    with pytest.raises(ValueError) as ei:
        aspirations.validate_verification({"outcomes_agent_leg": bad}, "g-305-04")
    msg = str(ei.value)
    assert "outcomes_agent_leg" in msg, f"error should name the field: {msg}"
    assert "g-305-04" in msg, f"error should name the goal: {msg}"


# --- validate_goal routes through to validate_verification -----------------

def test_validate_goal_accepts_good_agent_leg():
    """A well-formed goal carrying outcomes_agent_leg validates end-to-end."""
    aspirations.validate_goal({
        "id": "g-305-04", "status": "pending",
        "verification": {"outcomes": ["overall"],
                         "outcomes_agent_leg": ["agent leg done"]},
    })


def test_validate_goal_rejects_bad_agent_leg():
    """validate_goal must reach the new field's validation (not skip it)."""
    with pytest.raises(ValueError):
        aspirations.validate_goal({
            "id": "g-305-04", "status": "pending",
            "verification": {"outcomes_agent_leg": "not a list"},
        })


def _run_all():
    """Standalone runner (no pytest) -- manual case expansion."""
    failures = []

    def check(name, fn, expect=None):
        try:
            fn()
            if expect is None:
                print(f"PASS {name}")
            else:
                failures.append(name)
                print(f"FAIL {name}: expected {expect.__name__}, none raised")
        except Exception as e:  # noqa: BLE001
            if expect and isinstance(e, expect):
                print(f"PASS {name} ({type(e).__name__})")
            else:
                failures.append(name)
                print(f"FAIL {name}: {type(e).__name__}: {e}")

    check("absent",
          lambda: aspirations.validate_verification({"outcomes": ["x"]}, "g-000-00"))
    check("none",
          lambda: aspirations.validate_verification({"outcomes_agent_leg": None}, "g-000-00"))
    check("list",
          lambda: aspirations.validate_verification({"outcomes_agent_leg": ["a"]}, "g-000-00"))
    check("empty",
          lambda: aspirations.validate_verification({"outcomes_agent_leg": []}, "g-000-00"))
    for bad in ["s", {"k": 1}, 42, ("a",)]:
        check(f"bad={bad!r}",
              lambda b=bad: aspirations.validate_verification(
                  {"outcomes_agent_leg": b}, "g-305-04"), ValueError)
    check("goal-good",
          lambda: aspirations.validate_goal({
              "id": "g-305-04", "status": "pending",
              "verification": {"outcomes_agent_leg": ["done"]}}))
    check("goal-bad",
          lambda: aspirations.validate_goal({
              "id": "g-305-04", "status": "pending",
              "verification": {"outcomes_agent_leg": "x"}}), ValueError)

    print(f"\n{'-' * 30}")
    print("ALL PASS" if not failures else "FAILURES: " + ", ".join(failures))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(_run_all())
