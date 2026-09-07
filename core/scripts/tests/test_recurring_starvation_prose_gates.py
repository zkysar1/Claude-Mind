"""A prose precondition is UNEVALUATABLE, not passing ( remedy a).

`_structured_gates` keeps only dicts carrying a "type", because that is what
`predicate.evaluate_all` can evaluate. A STRING precondition is dropped, so a
string-only-gated goal reaches `_is_shelved`'s `if not gates` early return and is
reported NOT shelved however definitively its precondition fails. `shelved` is
then a structural constant for that goal (guard-1665 check 2), and the goal is
reported STARVED while it is in fact parked.

Measured four times across three agents before this pin: 82% of recurring goals
declaring any precondition were all-prose, and the three SHORTEST intervals in
that set were all in it, so the blindness concentrates where re-selection is most
frequent. Twice it cost a full iteration (claim -> execute -> close routine on a
goal whose gate was unmet); once it spent the drain lane's bounded top slot ahead
of 1,153 candidates, with a banner telling the agent to claim it without a
deviation code.

WHAT THIS CHANGE DOES NOT DO, pinned by
`test_string_gated_goal_is_still_not_shelved`: it does not reclassify anything. A
string is still unevaluatable and the goal is still reported starved. Converting
these to structured predicates is a separate and NOT-free change — the obvious
`command_succeeds` wrapper around an existing reader exits 0 on the empty case
and silently INVERTS the gate, and the empty case is reachable as BOTH null and
[]. What this adds is the ability to tell "evaluated and passing" from "could not
evaluate", exactly as `precondition-defer-recheck.py` already reports under
`skipped_free_form`.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

_spec = importlib.util.spec_from_file_location(
    "recurring_starvation_check",
    str(SCRIPT_DIR / "recurring-starvation-check.py"),
)
rsc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsc)

PROSE = "Working memory encoding_queue exists and has items"


def _ago(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).replace(
        microsecond=0).isoformat()


def _goal(**over) -> dict:
    g = {
        "id": "g-999-01",
        "title": "Recurring: synthetic sweep",
        "recurring": True,
        "status": "pending",
        "interval_hours": 6,
        "lastAchievedAt": _ago(20),
    }
    g.update(over)
    return g


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("MIND_AGENT", raising=False)
    monkeypatch.setattr(rsc, "_read_active", lambda source: [])


def _install(monkeypatch, goals):
    monkeypatch.setattr(
        rsc, "_read_active",
        lambda source: ([{"id": "asp-999", "goals": list(goals)}]
                        if source == "world" else []))


# ── the collector ────────────────────────────────────────────────────────

def test_prose_gates_returns_string_preconditions():
    g = _goal(verification={"preconditions": [PROSE]})
    assert rsc._prose_gates(g) == [PROSE]


def test_prose_gates_ignores_structured_preconditions():
    """Only difference from the test above: the entry is a dict, not a str."""
    g = _goal(verification={"preconditions": [{"type": "after_time", "value": "x"}]})
    assert rsc._prose_gates(g) == []


def test_prose_gates_ignores_blank_strings():
    g = _goal(verification={"preconditions": ["", "   "]})
    assert rsc._prose_gates(g) == []


def test_prose_and_structured_gates_partition_the_list():
    """Neither collector may claim an entry the other also claims, and together
    they must not silently drop a well-formed one — that dropped middle is the
    defect class this whole file is about."""
    structured = {"type": "after_time", "value": "x"}
    g = _goal(verification={"preconditions": [PROSE, structured]})
    prose, struct = rsc._prose_gates(g), rsc._structured_gates(g)
    assert prose == [PROSE]
    assert struct == [structured]
    assert len(prose) + len(struct) == 2


# ── the load-bearing safety property: classification is UNCHANGED ────────

def test_string_gated_goal_is_still_not_shelved():
    """Remedy (a) is REPORTING-only. If this ever flips, a prose precondition has
    started silently parking goals, which is the opposite failure and worse."""
    g = _goal(verification={"preconditions": [PROSE]})
    shelved, gate_type = rsc._is_shelved(g)
    assert shelved is False
    assert gate_type is None


# ── the reported signal ──────────────────────────────────────────────────

def test_starved_row_carries_the_unevaluatable_precondition(monkeypatch):
    _install(monkeypatch, [_goal(verification={"preconditions": [PROSE]})])
    starved, stats = rsc.scan(3.0, breaks={})
    assert len(starved) == 1, "fixture did not reach the starved branch"
    assert starved[0]["unevaluatable_preconditions"] == [PROSE]
    assert stats["gates_unevaluatable"] == 1


def test_starved_row_without_prose_reports_none(monkeypatch):
    """Positive control for the test above. Same goal, same starved verdict, no
    prose precondition — so an implementation that always populates the field or
    always bumps the counter fails here."""
    _install(monkeypatch, [_goal()])
    starved, stats = rsc.scan(3.0, breaks={})
    assert len(starved) == 1, "fixture did not reach the starved branch"
    assert starved[0]["unevaluatable_preconditions"] == []
    assert stats["gates_unevaluatable"] == 0
