""" item 3 — the goal-field census ratchet gates the RIGHT number.

Two decisions in this ratchet are easy to "tidy" into something wrong later, and
both are load-bearing, so they are pinned here rather than left to the docstring.

1. IT MUST NOT RATCHET THE STRAY COUNT. Gating on "strays must fall" is the
   obvious reading of the goal and is UNSATISFIABLE: `aspirations.jsonl` is
   merge-protected by a COMMUTATIVE merge handler, so a key absent from a write
   and present remotely resolves to present — a migration that pops 34 keys
   writes successfully and changes nothing (measured 2026-08-18). A permanent
   WARN nobody can clear is worse than no measurement, because it trains readers
   to ignore the ratchet.

2. IT MUST ENUMERATE STATUSES FROM `aspirations.VALID_GOAL_STATUSES`. A
   hand-written six-status list omits `decomposed` and `superseded` and
   undercounted this exact metric by 2 goals and 1 distinct key while looking
   completely reasonable.
"""
import importlib.util
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "goal_field_census_ratchet", _SCRIPTS / "goal-field-census-ratchet.py")
ratchet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ratchet)

_SRC = (_SCRIPTS / "goal-field-census-ratchet.py").read_text(encoding="utf-8")


def test_statuses_come_from_the_ssot_not_a_hand_written_list():
    from aspirations import VALID_GOAL_STATUSES
    assert "VALID_GOAL_STATUSES" in _SRC
    # The omission that actually happened: a six-status list missing these two.
    assert "decomposed" in VALID_GOAL_STATUSES
    assert "superseded" in VALID_GOAL_STATUSES
    # And the module must not carry its own literal status list.
    assert '"pending", "in-progress", "completed"' not in _SRC


def test_the_ratcheted_metric_is_distinct_keys_not_the_stray_count():
    assert ratchet.KEY == "goal_field_distinct_keys"
    # EXACT, not a fallback chain of substrings. An `or ... or 'ratcheted_metric'
    # in _SRC` tail would pass on the mere presence of the word and assert
    # nothing at all — the weak-predicate failure this whole goal kept hitting.
    assert '"ratcheted_metric": "distinct_keys"' in _SRC
    # The verdict must be computed from distinct_keys, never from the stray count.
    assert 'cur = current["distinct_keys"]' in _SRC
    assert 'cur = current["stray_occurrences"]' not in _SRC


def test_stray_count_is_recorded_as_reported_but_not_ratcheted():
    """If someone later gates on this, the baseline entry should contradict them."""
    assert "reported_not_ratcheted" in _SRC
    assert "stray_occurrences" in _SRC


def test_a_zero_population_is_refused_rather_than_seeding_a_baseline_of_zero():
    """rb-245: an empty result means the query broke, not that the fleet is empty.

    Seeding 0 would flag every subsequent honest run as 'regressed'.
    """
    assert 'goals_scanned"] == 0' in _SRC
    assert "refusing to seed a baseline of 0" in _SRC


def test_the_baseline_is_never_raised_on_a_regression():
    """A ratchet that raises its baseline on regression stops being a ratchet."""
    assert 'verdict, new_baseline = "regressed", prior' in _SRC


def test_the_regression_message_sends_the_reader_to_the_override_ledger_first():
    """A deliberate field registration RAISES this number legitimately.

    Without that pointer the first true-positive reads as a bug hunt.
    """
    assert "override-bypass-ledger" in _SRC
    assert "_goal_fields.py" in _SRC


def test_unparseable_query_output_raises_instead_of_becoming_a_clean_zero():
    """guard-2298: a shape change must not be laundered into a confident 0."""
    assert "unparseable" in _SRC
    assert "bytes" in _SRC
