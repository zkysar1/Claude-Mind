"""Regression pins for core/scripts/sq009_formation_gates.py (gap-071).

These are the Step 3.6 dogfood fixtures made permanent. The two defects being
pinned are not hypothetical — both shipped silently in this fleet:

  * g-115-4005 tallied the wrong key and tightened the calibration cap a full
    band without returning zero, so nothing looked broken.
  * guard-654: substring-matching "correct" counts CORRECTED (a MISS) as a hit,
    inverting the confirmed/corrected ratio.

The mutation test at the bottom is the load-bearing one: it asserts the suite
DISCRIMINATES at the value the caller actually consumes (the cap), not merely at
an intermediate tally. An earlier draft of these fixtures proved discrimination
at the tally while both variants mapped to the same cap — green through the very
defect it was written to catch (guard-1793).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sq009_formation_gates import _tally, cap_for  # noqa: E402


def _substring_tally(records):
    """The guard-654 defect, reproduced. 'CORRECTED' contains 'correct'."""
    hit = sum(1 for r in records if "correct" in (r.get("outcome") or "").lower())
    return hit, len(records) - hit


MIXED = (
    [{"outcome": "CONFIRMED"}] * 2
    + [{"outcome": "CORRECTED"}] * 3
    + [{"outcome": "UNRESOLVABLE"}]
)


def test_tally_matches_outcome_enum_exactly():
    assert _tally(MIXED) == (2, 3)


def test_unresolvable_excluded_from_denominator():
    # 6 records in, 5 scoreable out — UNRESOLVABLE is neither hit nor miss.
    assert sum(_tally(MIXED)) == 5


@pytest.mark.parametrize(
    "accuracy,expected_cap",
    [
        (0.0, 0.55),
        (0.399, 0.55),
        (0.40, 0.65),   # band boundary
        (0.599, 0.65),
        (0.60, 0.80),   # band boundary
        (0.799, 0.80),
        (0.80, None),   # band boundary — no cap at/above 0.80
        (1.0, None),
    ],
)
def test_cap_band_boundaries(accuracy, expected_cap):
    assert cap_for(accuracy) == expected_cap


def test_pass_and_fail_fixtures_drive_distinct_caps():
    """Anti-vacuity: a gate returning one verdict for both inputs is useless."""
    all_hit = [{"outcome": "CONFIRMED"}] * 10
    all_miss = [{"outcome": "CORRECTED"}] * 10
    h, m = _tally(all_hit)
    cap_pass = cap_for(h / (h + m))
    h, m = _tally(all_miss)
    cap_fail = cap_for(h / (h + m))
    assert cap_pass is None
    assert cap_fail == 0.55
    assert cap_pass != cap_fail


def test_substring_mutation_changes_the_consumed_cap():
    """guard-654 + guard-1793: discriminate at the CAP, not just the tally.

    7 CONFIRMED + 1 CORRECTED. Exact match -> 0.875 -> no cap. The substring
    defect inverts it to 1/8 -> 0.125 -> cap 0.55. Chosen precisely because a
    2C/3K fixture moves the tally while leaving BOTH variants in the 0.65 band,
    which would let this suite pass through the live defect.
    """
    fixture = [{"outcome": "CONFIRMED"}] * 7 + [{"outcome": "CORRECTED"}]

    h, m = _tally(fixture)
    correct_cap = cap_for(h / (h + m))

    bh, bm = _tally_broken = _substring_tally(fixture)
    broken_cap = cap_for(bh / (bh + bm))

    assert (h, m) == (7, 1)
    assert _tally_broken == (1, 7)          # fully inverted
    assert correct_cap is None
    assert broken_cap == 0.55
    assert correct_cap != broken_cap        # the defect moves the OUTPUT
