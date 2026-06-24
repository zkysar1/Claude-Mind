"""Tests for retrieval_utility_report.py (Phase 1d — learning KPI)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retrieval_utility_report as rur  # noqa: E402


def _rec(rid, retrieved, helpful, score):
    return {"id": rid, "utilization": {"retrieval_count": retrieved,
                                       "times_helpful": helpful,
                                       "utilization_score": score}}


def test_report_basic_stats():
    recs = [
        _rec("rb-1", retrieved=10, helpful=4, score=0.4),   # helpful, retrieved
        _rec("rb-2", retrieved=8, helpful=0, score=0.0),    # zero-hit high-exposure noise
        _rec("rb-3", retrieved=0, helpful=0, score=0.0),    # never retrieved
        _rec("rb-4", retrieved=2, helpful=1, score=0.5),    # helpful, low exposure
    ]
    r = rur.report(recs, high_exposure_min=5)
    assert r["n_total"] == 4 and r["n_tracked"] == 4
    assert r["hit_rate"] == pytest.approx(2 / 4)        # rb-1, rb-4
    assert r["retrieved_rate"] == pytest.approx(3 / 4)  # rb-1, rb-2, rb-4
    assert r["zero_hit_high_exposure"] == ["rb-2"]      # retrieved>=5, never helpful
    assert r["never_retrieved"] == ["rb-3"]
    assert r["mean_utilization_score"] == pytest.approx((0.4 + 0.0 + 0.0 + 0.5) / 4)


def test_report_untracked_records_counted_separately():
    recs = [_rec("rb-1", 5, 2, 0.4), {"id": "rb-legacy"}]  # second has no utilization
    r = rur.report(recs)
    assert r["n_total"] == 2 and r["n_tracked"] == 1
    assert r["hit_rate"] == 1.0  # only the tracked one counts


def test_report_survives_null_and_garbage_counters():
    # HIGH fresh-eyes finding: explicit null counters (common in real JSONL) must
    # not crash the report; they degrade to 0.
    recs = [
        {"id": "rb-1", "utilization": {"retrieval_count": None, "times_helpful": None,
                                       "utilization_score": None}},
        {"id": "rb-2", "utilization": {"retrieval_count": 9, "times_helpful": 0,
                                       "utilization_score": "garbage"}},
        {"id": "rb-3", "utilization": {"retrieval_count": 4, "times_helpful": 2,
                                       "utilization_score": 0.5}},
    ]
    r = rur.report(recs, high_exposure_min=5)  # must not raise
    assert r["n_tracked"] == 3
    assert r["never_retrieved"] == ["rb-1"]            # null -> 0 retrievals
    assert r["zero_hit_high_exposure"] == ["rb-2"]     # 9 retrievals, 0 helpful
    assert r["hit_rate"] == pytest.approx(1 / 3, abs=1e-4)   # only rb-3 helpful (rounded 4dp)
    assert r["mean_utilization_score"] == pytest.approx(0.5 / 3, abs=1e-4)


def test_report_no_tracked_records():
    r = rur.report([{"id": "rb-legacy"}])
    assert r["n_tracked"] == 0 and r["hit_rate"] is None and "note" in r


def test_high_exposure_threshold_respected():
    recs = [_rec("rb-1", retrieved=3, helpful=0, score=0.0)]
    # below threshold -> not flagged as confident noise (just not-yet-encountered)
    assert rur.report(recs, high_exposure_min=5)["zero_hit_high_exposure"] == []
    assert rur.report(recs, high_exposure_min=3)["zero_hit_high_exposure"] == ["rb-1"]


def test_load_records_skips_blanks_and_comments(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("# comment\n\n" + json.dumps(_rec("rb-1", 1, 1, 1.0)) + "\n",
                 encoding="utf-8")
    recs = rur.load_records(p)
    assert len(recs) == 1 and recs[0]["id"] == "rb-1"


def test_cli(tmp_path, capsys):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in
                           [_rec("rb-1", 10, 0, 0.0), _rec("rb-2", 1, 1, 1.0)]),
                 encoding="utf-8")
    rc = rur.main(["--store", str(p), "--high-exposure-min", "5"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["zero_hit_high_exposure"] == ["rb-1"]
