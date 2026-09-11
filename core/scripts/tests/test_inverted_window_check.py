"""Tests for inverted-window-check.py ().

The sweep flags non-terminal goals whose `deferred_until` was taken from
`resolves_by` instead of `resolves_no_earlier_than` — guard-3208's inverted
resolution window, which freezes a hypothesis goal for its entire span so it
becomes selectable only on the day it expires.

These tests pin the two things the sweep can get wrong in opposite directions:
the EXACT copy signature must fire on the real incident shapes, and it must NOT
fire on the three legitimate shapes that look similar (a correct gate, a
same-day window, and a later unrelated defer). The bucket SPLIT is pinned too —
folding `wide_gate` into `inverted` is the specific regression that would turn
this detective into a false-alarm generator, and guard-3628 is why the two are
reported apart rather than summed.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "inverted-window-check.py"


def _import():
    spec = importlib.util.spec_from_file_location("inverted_window_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["inverted_window_check"] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _import()


def _goal(**kw):
    """Canonical INVERTED goal, shaped from the  incident.

    resolves_no_earlier_than 2026-08-12 (window opens), resolves_by 2026-09-10
    (deadline), deferred_until copied from the deadline — so the goal is frozen
    for all 29 days and surfaces only on its last legal day.
    """
    g = {
        "id": "g-335-1110",
        "status": "pending",
        "deferred_until": "2026-09-10T00:00:00",
        "resolves_no_earlier_than": "2026-08-12",
        "resolves_by": "2026-09-10",
        "_source": "world",
        "_aspiration_id": "asp-335",
        "title": "Resolve hypothesis: efs live fidelity red is transport not fidelity",
    }
    g.update(kw)
    return g


# --------------------------------------------------------------------------
# The signature fires
# --------------------------------------------------------------------------

def test_canonical_incident_shape_is_inverted():
    """: the 29-day freeze guard-3208 was written a second time for."""
    assert MOD.classify(_goal()) == "inverted"


def test_mixed_date_and_timestamp_shapes_both_parse():
    """The live corpus mixes bare dates with timestamps in these three fields.

    g-350-216 carries a TIMED resolves_no_earlier_than (2026-08-15T04:00:00)
    against midnight deferred_until/resolves_by. A parser that handled only one
    shape would silently return None and the worst-frozen goal in the fleet
    (88 days) would never be flagged — the failure would look like a clean sweep.
    """
    g = _goal(id="g-350-216",
              resolves_no_earlier_than="2026-08-15T04:00:00",
              deferred_until="2026-11-12T00:00:00",
              resolves_by="2026-11-12T00:00:00")
    assert MOD.classify(g) == "inverted"


def test_blocked_status_is_still_non_terminal_and_flagged():
    assert MOD.classify(_goal(status="blocked")) == "inverted"
    assert MOD.classify(_goal(status="in-progress")) == "inverted"


# --------------------------------------------------------------------------
# The signature does NOT fire — the three legitimate look-alikes
# --------------------------------------------------------------------------

def test_gate_at_the_window_opening_is_correct_not_inverted():
    """deferred_until == resolves_no_earlier_than is exactly the RIGHT write.

    This is the shape guard-3208 prescribes. Flagging it would make the sweep
    fire on every correctly-gated goal, which is how a detector gets ignored.
    """
    g = _goal(deferred_until="2026-08-12T00:00:00")
    assert MOD.classify(g) is None


def test_gate_before_the_window_opening_is_not_flagged():
    assert MOD.classify(_goal(deferred_until="2026-08-01T00:00:00")) is None


def test_same_day_window_whose_gate_equals_both_dates_is_not_inverted():
    """A one-day window has rne == resolves_by, so a gate on that day is right.

    Without the `resolves_no_earlier_than < deferred_until` clause the exact
    copy-signature alone would flag this, because deferred_until does equal
    resolves_by. That is a false positive on a correct record.
    """
    g = _goal(resolves_no_earlier_than="2026-09-10",
              resolves_by="2026-09-10",
              deferred_until="2026-09-10T00:00:00")
    assert MOD.classify(g) is None


def test_later_unrelated_defer_is_wide_gate_advisory_never_inverted():
    """'s shape: gate BETWEEN the window opening and the deadline.

    A defer written later for an unrelated reason produces this legitimately, so
    it is reported as advisory and MUST NOT be counted as the defect.
    guard-3208's action_hint offers `deferred_until > resolves_no_earlier_than`
    as a 'more general' predicate; this is the row that shows why the general
    form is the wrong thing to act on.
    """
    g = _goal(id="g-115-9044",
              resolves_no_earlier_than="2026-09-06",
              deferred_until="2026-09-11T00:00:00",
              resolves_by="2026-10-05")
    assert MOD.classify(g) == "wide_gate"


def test_wide_gate_and_inverted_are_distinct_verdicts():
    """The split is the contract: folding them would inflate the actionable set.

    Negative control for the bucket boundary (guard-3628) — the same goal moves
    between buckets on the copy signature ALONE, with every other field fixed.
    """
    base = dict(id="g-x", status="pending",
                resolves_no_earlier_than="2026-08-01", resolves_by="2026-09-01")
    assert MOD.classify(dict(base, deferred_until="2026-09-01T00:00:00")) == "inverted"
    assert MOD.classify(dict(base, deferred_until="2026-08-20T00:00:00")) == "wide_gate"


# --------------------------------------------------------------------------
# Terminal, absent and malformed records never produce a flag
# --------------------------------------------------------------------------

def test_terminal_statuses_are_excluded():
    for status in ("completed", "skipped", "expired", "superseded", "decomposed"):
        assert MOD.classify(_goal(status=status)) is None, status


def test_missing_window_fields_never_flag():
    assert MOD.classify(_goal(resolves_no_earlier_than=None)) is None
    assert MOD.classify(_goal(deferred_until=None)) is None
    assert MOD.classify(_goal(deferred_until="", resolves_no_earlier_than="")) is None


def test_missing_resolves_by_downgrades_to_advisory_not_inverted():
    """No deadline means no copy signature to match — it cannot be `inverted`."""
    assert MOD.classify(_goal(resolves_by=None)) == "wide_gate"


def test_unparseable_dates_never_flag():
    """An unparseable date must return None, never manufacture a defect."""
    assert MOD.classify(_goal(deferred_until="soon")) is None
    assert MOD.classify(_goal(resolves_no_earlier_than="when the window opens")) is None
    assert MOD.classify(_goal(deferred_until="2026-13-45T99:99:99")) is None


def test_non_dict_input_is_tolerated():
    for junk in (None, "g-335-1110", 42, ["g-335-1110"]):
        assert MOD.classify(junk) is None


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

def test_parse_accepts_both_store_shapes():
    assert MOD._parse("2026-08-12") == dt.datetime(2026, 8, 12)
    assert MOD._parse("2026-08-15T04:00:00") == dt.datetime(2026, 8, 15, 4, 0, 0)
    assert MOD._parse("  2026-08-12  ") == dt.datetime(2026, 8, 12)
    assert MOD._parse(None) is None
    assert MOD._parse("") is None
    assert MOD._parse("not-a-date") is None


def test_entry_reports_frozen_days_and_still_frozen():
    """frozen_days is the span the goal is held past its own opening date."""
    e = MOD._entry(_goal(), dt.datetime(2026, 8, 20))
    assert e["frozen_days"] == 29
    assert e["still_frozen"] is True
    after = MOD._entry(_goal(), dt.datetime(2026, 9, 11))
    assert after["still_frozen"] is False
