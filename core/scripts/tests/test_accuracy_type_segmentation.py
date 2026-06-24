#!/usr/bin/env python3
"""test_accuracy_type_segmentation.py - regression test ( / rb-268).

Pins the accuracy gate's type-segmented overconfidence signal. Exploration
hypotheses are designed-uncertain probes; their low hit-rate must NOT drag the
calibration-relevant accuracy that gates accuracy_low. The gate flags on the
COMMITMENT types (everything except exploration), falling back to the aggregate
when by_type is absent (legacy pipeline meta).

Canonical incident (g-001-84): aggregate 37.5% (3/8) fired the gate while
high-conviction was 2/2=100% and the misses were all in exploration/calibration
designed-uncertain types.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# precheck-eval.py is hyphenated -> load by path
_spec_pe = importlib.util.spec_from_file_location("precheck_eval", SCRIPT_DIR / "precheck-eval.py")
pe = importlib.util.module_from_spec(_spec_pe)
_spec_pe.loader.exec_module(pe)

# pipeline.py -> load by path for consistency
_spec_pl = importlib.util.spec_from_file_location("pipeline", SCRIPT_DIR / "pipeline.py")
pl = importlib.util.module_from_spec(_spec_pl)
_spec_pl.loader.exec_module(pl)

CONFIG = {"accuracy_critical_threshold": 0.40, "accuracy_min_sample": 5}


def _run_accuracy(data, config=CONFIG):
    """Invoke cmd_accuracy with _pipeline_query monkeypatched to return data."""
    class Args:
        pass

    orig = pe._pipeline_query
    pe._pipeline_query = lambda *a, **k: data
    try:
        return pe.cmd_accuracy(Args(), config, {})
    finally:
        pe._pipeline_query = orig


def test_exploration_heavy_does_not_flag():
    """ shape: exploration-heavy aggregate below threshold, but the
    calibration-relevant sample is small -> no accuracy_low flag."""
    data = {
        "total_resolved": 8, "accuracy_pct": 37.5, "confirmed": 3,
        "by_strategy": {},
        "by_type": {
            "exploration": {"confirmed": 1, "total": 4, "pct": 25.0},
            "calibration": {"confirmed": 0, "total": 2, "pct": 0.0},
            "high-conviction": {"confirmed": 2, "total": 2, "pct": 100.0},
        },
    }
    r = _run_accuracy(data)
    assert "accuracy_low" not in r["flags"], f"exploration-heavy should NOT flag: {r}"
    assert r["flag_basis"] == "calibration-relevant", r
    # calibration-relevant = high-conviction(2) + calibration(2) = 4, excludes exploration(4)
    assert r["calibration_relevant_total"] == 4, r
    print("PASS: exploration-heavy aggregate does not flag (g-001-84 shape)")


def test_genuine_overconfidence_still_flags():
    """High-conviction hypotheses missing badly -> overconfidence drift flags,
    even when exploration accuracy is perfect (exploration is excluded)."""
    data = {
        "total_resolved": 12, "accuracy_pct": 58.3, "confirmed": 7,
        "by_strategy": {},
        "by_type": {
            "high-conviction": {"confirmed": 1, "total": 6, "pct": 16.7},
            "exploration": {"confirmed": 6, "total": 6, "pct": 100.0},
        },
    }
    r = _run_accuracy(data)
    assert "accuracy_low" in r["flags"], f"genuine high-conviction overconfidence MUST flag: {r}"
    assert r["flag_basis"] == "calibration-relevant", r
    assert r["calibration_relevant_total"] == 6, r
    print("PASS: genuine high-conviction overconfidence still flags (exploration excluded)")


def test_calibration_relevant_healthy_does_not_flag():
    """Strong commitment accuracy + terrible exploration -> no flag (the whole
    point: exploration noise must not drag the calibration signal)."""
    data = {
        "total_resolved": 16, "accuracy_pct": 31.3, "confirmed": 5,
        "by_strategy": {},
        "by_type": {
            "high-conviction": {"confirmed": 5, "total": 6, "pct": 83.3},
            "exploration": {"confirmed": 0, "total": 10, "pct": 0.0},
        },
    }
    r = _run_accuracy(data)
    assert "accuracy_low" not in r["flags"], f"healthy commitments should NOT flag despite bad exploration: {r}"
    assert r["calibration_relevant_pct"] == 83.3, r
    print("PASS: healthy commitment accuracy does not flag despite 0% exploration")


def test_legacy_no_by_type_falls_back_to_aggregate():
    """Missing by_type (legacy meta) -> aggregate behavior preserved."""
    data = {
        "total_resolved": 8, "accuracy_pct": 30.0, "confirmed": 2,
        "by_strategy": {}, "by_type": {},
    }
    r = _run_accuracy(data)
    assert "accuracy_low" in r["flags"], f"legacy aggregate 30%/n8 should flag: {r}"
    assert r["flag_basis"] == "aggregate", r
    print("PASS: legacy no-by_type falls back to aggregate flag")


def test_compute_meta_emits_by_type():
    """pipeline.compute_meta populates accuracy.by_type symmetric to by_depth."""
    items = [
        {"id": "h1", "outcome": "CONFIRMED", "type": "high-conviction"},
        {"id": "h2", "outcome": "CONFIRMED", "type": "high-conviction"},
        {"id": "h3", "outcome": "CORRECTED", "type": "exploration"},
        {"id": "h4", "outcome": "CORRECTED", "type": "calibration"},
    ]
    meta = pl.compute_meta(items, [])
    bt = meta["accuracy"]["by_type"]
    assert bt["high-conviction"] == {"confirmed": 2, "total": 2, "pct": 100.0}, bt
    assert bt["exploration"] == {"confirmed": 0, "total": 1, "pct": 0.0}, bt
    assert bt["calibration"] == {"confirmed": 0, "total": 1, "pct": 0.0}, bt
    print("PASS: compute_meta emits accuracy.by_type")


def test_compute_meta_emits_by_confidence_band():
    """pipeline.compute_meta buckets resolved records by confidence band
    (g-001-122 / rb-323): high>=0.80, medium 0.65-0.79, low<0.65. Records
    without a numeric confidence are skipped."""
    items = [
        {"id": "h1", "outcome": "CONFIRMED", "type": "high-conviction", "confidence": 0.9},
        {"id": "h2", "outcome": "CORRECTED", "type": "high-conviction", "confidence": 0.85},
        {"id": "h3", "outcome": "CONFIRMED", "type": "calibration", "confidence": 0.7},
        {"id": "h4", "outcome": "CORRECTED", "type": "exploration", "confidence": 0.55},
        {"id": "h5", "outcome": "CORRECTED", "type": "exploration"},  # no confidence -> skipped
    ]
    meta = pl.compute_meta(items, [])
    bb = meta["accuracy"]["by_confidence_band"]
    # high: 2 records (0.9 confirmed, 0.85 corrected) -> 1/2 = 50%
    assert bb["high"] == {"confirmed": 1, "total": 2, "pct": 50.0}, bb
    # medium: 1 record (0.7 confirmed) -> 100%
    assert bb["medium"] == {"confirmed": 1, "total": 1, "pct": 100.0}, bb
    # low: 1 record (0.55 corrected) -> 0%
    assert bb["low"] == {"confirmed": 0, "total": 1, "pct": 0.0}, bb
    # the no-confidence record is excluded from every band
    assert sum(b["total"] for b in bb.values()) == 4, bb
    print("PASS: compute_meta emits accuracy.by_confidence_band")


def test_cmd_accuracy_surfaces_by_confidence_band():
    """cmd_accuracy passes through the daemon-computed by_confidence_band so the
    accuracy subcommand output shows WHERE overconfidence concentrates
    (g-001-122). Surfacing is independent of the flag basis."""
    data = {
        "total_resolved": 13, "accuracy_pct": 38.5, "confirmed": 5,
        "by_strategy": {},
        "by_type": {
            "high-conviction": {"confirmed": 2, "total": 4, "pct": 50.0},
            "exploration": {"confirmed": 1, "total": 5, "pct": 20.0},
            "calibration": {"confirmed": 2, "total": 4, "pct": 50.0},
        },
        "by_confidence_band": {
            "high": {"confirmed": 1, "total": 4, "pct": 25.0},
            "medium": {"confirmed": 2, "total": 3, "pct": 66.7},
            "low": {"confirmed": 2, "total": 6, "pct": 33.3},
        },
    }
    r = _run_accuracy(data)
    assert r["by_confidence_band"] == data["by_confidence_band"], r
    # legacy meta lacking the field surfaces an empty dict, not a KeyError
    r2 = _run_accuracy({"total_resolved": 8, "accuracy_pct": 30.0, "confirmed": 2,
                        "by_strategy": {}, "by_type": {}})
    assert r2["by_confidence_band"] == {}, r2
    print("PASS: cmd_accuracy surfaces by_confidence_band (empty for legacy meta)")


if __name__ == "__main__":
    test_exploration_heavy_does_not_flag()
    test_genuine_overconfidence_still_flags()
    test_calibration_relevant_healthy_does_not_flag()
    test_legacy_no_by_type_falls_back_to_aggregate()
    test_compute_meta_emits_by_type()
    test_compute_meta_emits_by_confidence_band()
    test_cmd_accuracy_surfaces_by_confidence_band()
    print()
    print("ALL 7 ACCURACY TYPE-SEGMENTATION TESTS PASS")
