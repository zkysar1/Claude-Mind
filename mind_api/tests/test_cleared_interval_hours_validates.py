"""A CLEARED interval_hours (present-but-None) must validate ().

Both validators gated on KEY PRESENCE, not on value-is-set:

    if "interval_hours" in goal:
        v = goal["interval_hours"]
        if not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(...)

`None` is not int/float, so a field that had been CLEARED raised. The perverse
consequence, measured live on g-326-802 (bravo, cc-05, 2026-09-02): a goal that
NEVER HAD the key validated fine, while a goal correctly RETIRED per the
documented recurring-retirement sequence could not change status AT ALL.

The scope is wider and narrower at once, and this module pins both halves:

* WIDER  — no status write of ANY kind succeeded. `completed` AND `blocked`
  both raised, so an affected goal could not even be parked as blocked. A fix
  special-casing only the completion path would leave the blocked transition
  broken, so both transitions are tested.
* NARROWER — non-status writes were unaffected (progress_note and blocker_ref
  both landed on the same goal in the same minute), which is why the defect
  survived: the goal was demonstrably writable.

The retirement sequence that produces the state is prescribed verbatim by
run-processor MONITOR's COMPLETED branch, and it clears the fields
deliberately, to avoid the `recurring=false` + `interval_hours` +
`lastAchievedAt` shape-corruption that `find_shape_recurring_corrupted`
recovers (rb-295, g-001-138). So the SKILL.md-prescribed retirement and the
validator disagreed, and the retirement path was the one that lost — the
guard-5091 shape (validator predicate vs store predicate, store is the clean
one).

TWO SITES, ONE DEFECT (guard-3275 / guard-742). The daemon validator
(`mind_api/src/endpoints/aspirations_write.py::_validate_goal`) and the CLI
validator (`core/scripts/aspirations.py::validate_goal`) both carried this
predicate verbatim. Fixing one leaves the other rejecting what its twin
accepts, which relocates the wedge instead of clearing it — so every case
below is asserted against BOTH, and the parity is asserted at the end.

⚠ The parity assertion is scoped to the `interval_hours` axis ONLY, and must
not be widened into a claim of general CLI/daemon equivalence — the two
validators are NOT globally parallel. Measured on the live corpus while
closing this goal (bravo, cc-05, 2026-09-03): across 2,869 active goals the
daemon validator raised on ZERO and the CLI validator raised on EIGHTEEN,
over five check families the daemon does not implement at all (prose-only
verification drift, the `user_leg_scope` enum, the `goal_source` enum,
`abstained_by` typing, `depends_on`/`blocked_by` consistency). That gap is a
separate, already-open finding — g-115-7018, "Census the daemon/CLI
validator-parity gap" — and is deliberately NOT addressed here. Widening
`test_cli_and_daemon_agree` to a full goal record would fail on that gap for
reasons unrelated to this defect, so it asserts over a minimal record.

The same measurement bounds the blast radius of the fix itself: of those
2,869 goals exactly ONE carried `interval_hours: null` — g-326-802, the goal
whose wedge produced this test. 2,756 do not carry the key and 112 carry a
positive value, so no live record changes verdict except the wedged one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "core" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mind_api.src.endpoints.aspirations_write import _validate_goal  # noqa: E402
import aspirations as _cli  # noqa: E402


def _goal(**over):
    g = {"id": "g-326-802", "status": "pending"}
    g.update(over)
    return g


def _validators():
    """The two mirrored validators, named for legible assertion failures."""
    return [("daemon", _validate_goal), ("cli", _cli.validate_goal)]


# --------------------------------------------------------------------------
# The regression: a CLEARED field validates, on BOTH status transitions
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["completed", "blocked"])
@pytest.mark.parametrize("name,fn", _validators())
def test_cleared_interval_hours_accepted_on_both_transitions(name, fn, status):
    """The exact wedge: retired goal, cleared fields, changing status.

    `blocked` is not decoration — it is the half that proves the fix is in the
    validator rather than in the completion endpoint. Before the fix an
    affected goal could not even be parked as blocked.
    """
    goal = _goal(
        status=status,
        recurring=False,
        interval_hours=None,
        lastAchievedAt=None,
    )
    fn(goal)  # must not raise


@pytest.mark.parametrize("name,fn", _validators())
def test_absent_interval_hours_still_validates(name, fn):
    """The sibling that always worked — it must keep working.

    This asymmetry (never-had-the-key passes, correctly-cleared fails) IS the
    defect's signature, so the passing side is worth pinning too.
    """
    fn(_goal(recurring=False))  # must not raise


# --------------------------------------------------------------------------
# The fix must NOT relax the real range check
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, -0.5, "6", "", [], {}, object()])
@pytest.mark.parametrize("name,fn", _validators())
def test_set_interval_hours_still_range_checked(name, fn, bad):
    """0, negative and non-numeric must still be rejected when the key IS set.

    `is not None` is a narrower gate than `in`, so it would be easy to widen it
    into "skip whenever falsy" — which would silently admit 0 and "". The empty
    string and 0 cases are the ones that would slip through such a slip.
    """
    with pytest.raises(ValueError, match="interval_hours"):
        fn(_goal(recurring=True, interval_hours=bad))


@pytest.mark.parametrize("good", [1, 6, 24, 0.5, 168])
@pytest.mark.parametrize("name,fn", _validators())
def test_positive_interval_hours_still_accepted(name, fn, good):
    """Positive control — a live recurring goal must keep validating."""
    fn(_goal(recurring=True, interval_hours=good))  # must not raise


# --------------------------------------------------------------------------
# CLI/daemon parity (guard-742): the two must never disagree
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "interval,should_raise",
    [
        (None, False),   # cleared — the regression
        (6, False),      # live recurring
        (0.5, False),    # sub-hour cadences are legal
        (0, True),
        (-1, True),
        ("6", True),
        ("", True),
    ],
)
def test_cli_and_daemon_agree(interval, should_raise):
    """Assert the two validators reach the SAME verdict on each input.

    A parity test is what makes the next single-site edit fail loudly instead
    of quietly re-opening the split this goal closed. Without it, a future fix
    to one mirror passes its own suite while the other keeps rejecting.
    """
    verdicts = {}
    for name, fn in _validators():
        try:
            fn(_goal(recurring=True, interval_hours=interval))
            verdicts[name] = "accept"
        except ValueError:
            verdicts[name] = "reject"

    expected = "reject" if should_raise else "accept"
    assert verdicts == {"daemon": expected, "cli": expected}, (
        f"CLI/daemon validator split on interval_hours={interval!r}: {verdicts}"
    )
