"""Regression tests for the CALIBRATION tree merge class ().

WHAT BROKE. `accuracy` matched no named class in `_classify_tree_field`, so it
defaulted to BASE, which rides the newer-`last_updated` LWW base. But
`last_updated` deliberately does NOT advance on a calibration edit (g-115-1683
reserves it for .md article freshness), so an accuracy-only write advanced NO
timestamp the merge keys on and was STRUCTURALLY unable to win. g-115-4410
measured the full chain on one box: the write LANDS, and one `read_tree` cycle
later it is REVERTED, forever — 12 of 12 nodes replanned the identical downgrade
every run.

WHY NOT JUST ADD IT TO PROGRESSION (the smallest diff, and it is WRONG).
`_merge_field_progression` carries never-regress-on-tie, which picks the HIGHER
value when the stamps are equal. The stamp is DATE-granular, so same-day
cross-box recalcs tie routinely — and on a tie the higher accuracy is
systematically the STALER one (the corpus grows, so recomputed accuracy drifts
down). That reproduces g-115-5856 in a new place. A data-derived DOWNGRADE is
the normal, correct result and must land (guard-1153).

The invariant every case here also asserts is COMMUTATIVITY: merge(a, b) must
equal merge(b, a), or the two machines never converge.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402


def _node(accuracy, calib_stamp=None, sample_size=None,
          last_updated="2026-08-01", **extra):
    n = {"accuracy": accuracy, "last_updated": last_updated}
    if calib_stamp is not None:
        n["calibration_updated_at"] = calib_stamp
    if sample_size is not None:
        n["sample_size"] = sample_size
    n.update(extra)
    return n


def _both_ways(a, b, field="accuracy"):
    """Merge in both argument orders; assert commutativity; return the value."""
    ab = cm._merge_tree_node(a, b)
    ba = cm._merge_tree_node(b, a)
    assert ab == ba, "merge is not commutative — the fleet cannot converge"
    return ab.get(field)


# --- classification ---------------------------------------------------------

def test_accuracy_is_calibration_not_base_and_not_progression():
    assert cm._classify_tree_field("accuracy") == "CALIBRATION"
    assert "accuracy" in cm._TREE_CALIBRATION_FIELDS
    assert "accuracy" not in cm._TREE_PROGRESSION_FIELDS
    assert "accuracy" not in cm._TREE_MAX_FIELDS


def test_calibration_stamp_is_a_newer_field_and_distinct_from_progression():
    # The stamp must itself merge NEWER, or the winning side's stamp is lost.
    assert cm._classify_tree_field("calibration_updated_at") == "NEWER"
    # DELIBERATELY separate from progression_updated_at (guard-3358): one
    # selector key serving two field groups with different write triggers would
    # let an accuracy-only edit advance the PROGRESSION LWW key.
    assert "calibration_updated_at" in cm._TREE_NEWER_FIELDS
    assert "progression_updated_at" in cm._TREE_NEWER_FIELDS
    assert "calibration_updated_at" != "progression_updated_at"


# --- the defect this class exists to fix ------------------------------------

def test_newer_stamp_wins_so_a_downgrade_lands():
    """The  reproduction, at the merge layer."""
    fresh_downgrade = _node(0.6, "2026-08-11", 10)
    stale_higher = _node(0.75, "2026-08-05", 10)
    assert _both_ways(fresh_downgrade, stale_higher) == 0.6


def test_newer_stamp_wins_for_an_upgrade_too():
    """Symmetric: the class is plain LWW, not never-progress either."""
    fresh_upgrade = _node(0.9, "2026-08-11", 10)
    stale_lower = _node(0.4, "2026-08-05", 10)
    assert _both_ways(fresh_upgrade, stale_lower) == 0.9


def test_last_updated_alone_cannot_decide_it_anymore():
    """Pre-fix, BASE rode last_updated — which a calibration edit never bumps,
    so the two sides tied there and the base pick was effectively arbitrary.
    With a stamp present, an OLDER last_updated on the freshly-recalculated side
    must not lose."""
    fresh_calc_old_article = _node(0.5, "2026-08-11", 50, last_updated="2026-04-18")
    stale_calc_new_article = _node(0.8, "2026-08-01", 50, last_updated="2026-08-10")
    assert _both_ways(fresh_calc_old_article, stale_calc_new_article) == 0.5


# --- the same-day tie, which is the COMMON case on a date-granular stamp ----

def test_same_day_tie_breaks_on_sample_size_allowing_a_downgrade():
    """Larger sample_size = computed over more records = the more current value.
    Under PROGRESSION's never-regress tie this would wrongly return 0.60."""
    more_data_lower = _node(0.55, "2026-08-11", 120)
    less_data_higher = _node(0.60, "2026-08-11", 100)
    assert _both_ways(more_data_lower, less_data_higher) == 0.55


def test_same_day_tie_breaks_on_sample_size_allowing_an_upgrade():
    more_data_higher = _node(0.70, "2026-08-11", 120)
    less_data_lower = _node(0.60, "2026-08-11", 100)
    assert _both_ways(more_data_higher, less_data_lower) == 0.70


def test_same_day_tie_with_equal_sample_size_is_deterministic():
    a = _node(0.55, "2026-08-11", 100)
    b = _node(0.60, "2026-08-11", 100)
    v = _both_ways(a, b)
    assert v in (0.55, 0.60)  # content tiebreak — the CONTRACT is determinism


def test_sample_size_present_on_only_one_side_is_used():
    with_n = _node(0.55, "2026-08-11", 100)
    without_n = _node(0.60, "2026-08-11", None)
    assert _both_ways(with_n, without_n) == 0.55


@pytest.mark.parametrize("bad", [True, False, "12", None])
def test_non_numeric_sample_size_does_not_crash_or_break_commutativity(bad):
    """bool is explicitly excluded from the numeric check — True must not act
    as sample_size 1."""
    a = _node(0.55, "2026-08-11", None)
    a["sample_size"] = bad
    b = _node(0.60, "2026-08-11", 100)
    assert _both_ways(a, b) in (0.55, 0.60)


# --- backfill safety: 0 of 1379 live nodes carried the stamp at fix time -----

def test_no_stamp_on_either_side_falls_back_to_last_updated():
    newer_article = _node(0.6, None, 10, last_updated="2026-08-11")
    older_article = _node(0.75, None, 10, last_updated="2026-08-05")
    assert _both_ways(newer_article, older_article) == 0.6


def test_stamp_on_one_side_only_beats_the_unstamped_side_when_newer():
    stamped = _node(0.6, "2026-08-11", 10, last_updated="2026-01-01")
    unstamped = _node(0.75, None, 10, last_updated="2026-08-05")
    assert _both_ways(stamped, unstamped) == 0.6


def test_accuracy_present_on_only_one_side_is_kept():
    """An absent field must never clobber a present one."""
    has = _node(0.6, "2026-08-11", 10)
    lacks = {"last_updated": "2026-08-20"}
    assert _both_ways(has, lacks) == 0.6


# --- the neighbouring classes must be untouched -----------------------------

def test_progression_is_unaffected_by_the_calibration_stamp():
    """An accuracy-only edit must NOT decide a confidence merge. This is the
    reason for a separate stamp rather than reusing progression_updated_at."""
    a = {"confidence": 0.3, "accuracy": 0.5, "last_updated": "2026-08-01",
         "calibration_updated_at": "2026-08-11",
         "progression_updated_at": "2026-08-01", "sample_size": 10}
    b = {"confidence": 0.9, "accuracy": 0.5, "last_updated": "2026-08-01",
         "calibration_updated_at": "2026-08-01",
         "progression_updated_at": "2026-08-05", "sample_size": 10}
    merged = cm._merge_tree_node(a, b)
    assert cm._merge_tree_node(b, a) == merged
    # b's progression stamp is newer, so b's confidence wins REGARDLESS of a's
    # newer calibration stamp.
    assert merged["confidence"] == 0.9
    # ...while a's newer calibration stamp still governs its own class.
    assert merged["calibration_updated_at"] == "2026-08-11"


def test_sample_size_still_merges_max():
    """sample_size stays MAX-class here; its own downgrade problem is latent and
    tracked separately (g-115-4410 correction 1) — this pins that this change did
    NOT silently alter it."""
    a = _node(0.5, "2026-08-11", 10)
    b = _node(0.5, "2026-08-11", 99)
    assert cm._merge_tree_node(a, b)["sample_size"] == 99
    assert cm._merge_tree_node(b, a)["sample_size"] == 99


# --- CLI <-> daemon stamp parity (the live write path is the daemon) --------

def test_cli_and_daemon_calibration_stamp_fields_match():
    """tree.py and mind_api/src/world/tree_write.py both declare the stamp
    field-tuple and both MUST match coordination_merge's SSOT. A drift here makes
    the fix inert on whichever path is not covered — and the daemon is the LIVE
    one (no-python-cli-fallback)."""
    import re
    root = Path(__file__).resolve().parent.parent.parent
    pat = re.compile(r'^_CALIBRATION_STAMP_FIELDS\s*=\s*(\([^)]*\))', re.M)
    seen = {}
    for rel in ("scripts/tree.py", "../mind_api/src/world/tree_write.py"):
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        m = pat.search(text)
        assert m, f"_CALIBRATION_STAMP_FIELDS not found in {rel}"
        seen[rel] = m.group(1)
    assert len(set(seen.values())) == 1, f"CLI/daemon stamp drift: {seen}"
    # ...and both must equal the merge-side SSOT.
    declared = next(iter(seen.values()))
    for f in cm._TREE_CALIBRATION_FIELDS:
        assert f'"{f}"' in declared, f"{f} missing from the stamp tuple {declared}"
