"""Pin the contract of `_sole_goal_id` in unblock-parent-status-sweep ().

WHY THIS FILE EXISTS. `_sole_goal_id` had ZERO test references anywhere in the
tree — measured across 1,088 test files / 13.5 MB, with `_parse_parent_id` (16
refs) and `_successor_marker_guard` (12 refs) as positive controls, so the zero
was a real absence and not a broken probe.

It got that way through a merge, not through neglect. The evil-merge audit
(g-115-2473, window 2026-08-11 -> 08-18) flagged merge 7308d11d7
("reconcile cc-07 fork window, 231 behind / 343 ahead, 4-file TRUE conflict").
Parent ^1 (4cad81bbb) carried `core/scripts/tests/test_unblock_parent_status_sweep_parse_rate.py`
— 9,414 B, 11 tests, 2 of them referencing the then-current helper
`_distinct_title_ids`. Parent ^2 (a5c6ab1ca) did not carry that file. The merge
took ^2, so the file is absent at HEAD and its 11 tests went with it.

THE PRODUCTION CONCEPT SURVIVED; THE COVERAGE DID NOT. That is the whole point.
`_distinct_title_ids(title) -> [ids]` was superseded by
`_sole_goal_id(text) -> id | None`. Both encode the same guard-2201 safety
property — refuse on more than one id rather than pick by position, because
"... blocked by g-A, needed for g-B" has a blocker and a beneficiary and
choosing either is a coin flip that sweeps a goal against the wrong parent's
status. So every content-diff step of the audit ladder correctly reads BENIGN
SUPERSESSION, and the touched files' suites are green, so the ladder's step 4
(run the suite) reads clean too. A function with no tests cannot go red.

Note the successor is NOT a pure rename: `_distinct_title_ids` deliberately
PRESERVED the zero-vs-many distinction ("only one of them is recoverable"),
while `_sole_goal_id` deliberately COLLAPSES both to None and leaves the
distinction to the caller. That narrowing is a legitimate design choice — it is
recorded here so a future reader does not mistake it for drift, and so the
property that IS load-bearing (never guess between two ids) is pinned.

CALL SHAPE IS PRODUCTION'S, not the contract-ideal one (guard-920): the single
production call site is `_sole_goal_id(os_) or _sole_goal_id(title)`, i.e. a raw
origin-signal string and a raw title string, so the fixtures below are those two
shapes rather than bare ids. The real function is imported and exercised — no
re-implementation of its regex in the probe (guard-4323).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SWEEP = SCRIPTS / "unblock-parent-status-sweep.py"


def _import_sweep():
    spec = importlib.util.spec_from_file_location("unblock_parent_status_sweep", SWEEP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sweep():
    return _import_sweep()


def test_exactly_one_id_is_returned(sweep):
    """The recoverable case: one id anywhere in the text."""
    assert sweep._sole_goal_id("Unblock: re-derive g-354-21's user leg") == "g-354-21"
    assert sweep._sole_goal_id("unblock:recurring-starved-g-115-6337") == "g-115-6337"


def test_zero_ids_returns_none(sweep):
    assert sweep._sole_goal_id("Unblock: the daemon will not start") is None
    assert sweep._sole_goal_id("") is None
    assert sweep._sole_goal_id(None) is None


def test_two_distinct_ids_refuses_rather_than_picking(sweep):
    """THE LOAD-BEARING PROPERTY (guard-2201).

    This is the exact shape both the dropped `_distinct_title_ids` docstring and
    the surviving `_sole_goal_id` docstring name: the two ids are a blocker and
    a beneficiary, position does not rank them, and picking either sweeps a goal
    against the wrong parent's status. Returning the FIRST id would pass a naive
    "did we parse an id?" test while being a coin flip here.
    """
    assert sweep._sole_goal_id("Unblock: blocked by g-115-100, needed for g-115-200") is None
    # order-independent: the refusal is not an artifact of which id comes first
    assert sweep._sole_goal_id("Unblock: blocked by g-115-200, needed for g-115-100") is None


def test_the_same_id_twice_is_not_ambiguous(sweep):
    """Distinctness, not raw occurrence count, is what makes a title ambiguous.

    Inherited from the dropped file's `test_duplicate_id_mentioned_twice_is_not_ambiguous`.
    A counting implementation that refused on two MENTIONS would drop a title
    that is perfectly recoverable, and would do so silently — the caller only
    ever sees None.
    """
    assert sweep._sole_goal_id("Unblock: g-115-6337 — see g-115-6337 for context") == "g-115-6337"


def test_three_distinct_ids_also_refuses(sweep):
    """Guards the >2 case: a threshold written as `== 2` would admit this."""
    assert sweep._sole_goal_id("g-1-1 and g-2-2 and g-3-3") is None


def test_positive_control_the_pattern_can_match_at_all(sweep):
    """Without this, every assertion above would pass against a matcher that
    found NOTHING — three of the five expect None. A file whose green depends on
    a predicate always returning None is not a test of that predicate.
    """
    assert sweep.GOAL_ID_EMBEDDED_PATTERN.findall("g-354-21") == ["g-354-21"]
    assert sweep._sole_goal_id("g-354-21") == "g-354-21"
