"""Tests for eval_signal_scorer.py (, earn-the-keep Phase 1 / G3).

Hermetic: imports the pure module directly (no bound agent, no daemon, no
world/meta). conftest.py inserts core/scripts on sys.path. A small SYNTHETIC
corpus is written to tmp_path — the tests do NOT depend on the live
meta/eval/cases.jsonl.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import eval_signal_scorer as ess  # noqa: E402


# Synthetic corpus: 2 good, 3 bad (one bad is full/targeted-divergent).
_ROWS = [
    {"id": "good-fix", "baseline_score": 0.9, "tags": ["framework-edit"],
     "signal_source": "test-pass-rate"},
    {"id": "good-deploy", "baseline_score": 0.85, "tags": ["deploy"],
     "signal_source": "deploy-verify"},
    {"id": "bad-regression", "baseline_score": 0.1, "tags": ["framework-edit-regression"],
     "signal_source": "test-pass-rate"},
    {"id": "bad-divergent", "baseline_score": 0.1, "tags": ["framework-edit-regression"],
     "signal_source": "test-pass-rate", "targeted_divergent": True},
    {"id": "bad-revert", "baseline_score": 0.1, "tags": ["deploy-revert"],
     "signal_source": "deploy-verify"},
    # unlabeled — excluded from the calibration metric
    {"id": "unlabeled", "baseline_score": None, "tags": ["framework-edit"],
     "signal_source": "verification-checks"},
]


def _corpus(tmp_path):
    p = tmp_path / "cases.jsonl"
    p.write_text("# header comment\n"
                 + "\n".join(json.dumps(r) for r in _ROWS), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# disposition + reconstruction
# --------------------------------------------------------------------------- #

def test_disposition_regression_and_revert_are_negative():
    assert ess._disposition({"tags": ["framework-edit-regression"]}) == -1
    assert ess._disposition({"tags": ["deploy-revert"]}) == -1
    assert ess._disposition({"tags": ["framework-edit"]}) == 1
    assert ess._disposition({"tags": ["deploy"]}) == 1
    assert ess._disposition({"tags": []}) == 1


def test_reconstruct_full_signal_sign_tracks_disposition():
    good = {"tags": ["framework-edit"], "signal_source": "test-pass-rate"}
    bad = {"tags": ["framework-edit-regression"], "signal_source": "test-pass-rate"}
    b_g, a_g = ess.reconstruct_signal(good, mode="full")
    b_b, a_b = ess.reconstruct_signal(bad, mode="full")
    assert a_g - b_g > 0      # good change -> signal up
    assert a_b - b_b < 0      # regression -> signal down


def test_targeted_mode_mis_passes_divergent_case_only():
    div = {"tags": ["framework-edit-regression"], "signal_source": "test-pass-rate",
           "targeted_divergent": True}
    # full suite still sees the regression (negative); targeted sees the passing
    # targeted test (positive) -> the naive gate mis-passes it.
    assert ess.reconstruct_signal(div, mode="full")[1] - ess.reconstruct_signal(div, mode="full")[0] < 0
    bt, at = ess.reconstruct_signal(div, mode="targeted")
    assert at - bt > 0
    # a NON-divergent regression is negative under BOTH modes
    nd = {"tags": ["framework-edit-regression"], "signal_source": "test-pass-rate"}
    assert ess.reconstruct_signal(nd, mode="targeted")[1] - ess.reconstruct_signal(nd, mode="targeted")[0] < 0


def test_unknown_signal_source_raises():
    with pytest.raises(ValueError):
        ess.reconstruct_signal({"tags": [], "signal_source": "made-up"}, mode="full")


def test_bad_mode_raises():
    with pytest.raises(ValueError):
        ess.reconstruct_signal({"tags": [], "signal_source": "deploy-verify"}, mode="sideways")


# --------------------------------------------------------------------------- #
# records / calibration
# --------------------------------------------------------------------------- #

def test_build_records_excludes_unlabeled(tmp_path):
    rows = ess._read_raw(_corpus(tmp_path))
    recs = ess.build_records(rows, mode="full")
    ids = {r["id"] for r in recs}
    assert "unlabeled" not in ids
    assert len(recs) == 5


def test_evaluate_calibration_trust_logic():
    recs = [
        {"id": "g", "baseline_score": 0.9, "signal_delta": 0.1},
        {"id": "b", "baseline_score": 0.1, "signal_delta": -0.1},
    ]
    rep = ess.evaluate_calibration(recs)
    assert rep["true_pass_rate"] == 1.0
    assert rep["true_block_rate"] == 1.0
    assert rep["trustworthy"] is True
    assert rep["misses"] == []


def test_evaluate_calibration_surfaces_misses():
    recs = [
        {"id": "g", "baseline_score": 0.9, "signal_delta": -0.1},   # good wrongly blocked
        {"id": "b", "baseline_score": 0.1, "signal_delta": +0.1},   # bad wrongly passed
    ]
    rep = ess.evaluate_calibration(recs)
    assert rep["true_pass_rate"] == 0.0
    assert rep["true_block_rate"] == 0.0
    assert rep["trustworthy"] is False
    assert set(rep["misses"]) == {"g", "b"}


# --------------------------------------------------------------------------- #
# keystone end-to-end
# --------------------------------------------------------------------------- #

def test_keystone_full_is_trustworthy(tmp_path):
    rep = ess.run_keystone(_corpus(tmp_path), mode="full")
    assert rep["n_good"] == 2 and rep["n_bad"] == 3
    assert rep["true_pass_rate"] == 1.0
    assert rep["true_block_rate"] == 1.0
    assert rep["trustworthy"] is True


def test_keystone_targeted_mis_passes_divergent(tmp_path):
    rep = ess.run_keystone(_corpus(tmp_path), mode="targeted")
    # the divergent bad case is mis-passed -> it appears in misses, tbr drops
    assert "bad-divergent" in rep["misses"]
    assert rep["true_block_rate"] < 1.0


def test_signal_source_matters_is_set_difference(tmp_path):
    cases = _corpus(tmp_path)
    full = ess.run_keystone(cases, mode="full")
    targeted = ess.run_keystone(cases, mode="targeted")
    targeted_only = set(targeted["misses"]) - set(full["misses"])
    # the full suite catches the divergent regression that targeted mis-passes
    assert targeted_only == {"bad-divergent"}


def test_empty_corpus_raises(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("# only a comment\n\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ess._read_raw(p)


def test_duplicate_ids_raise(tmp_path):
    p = tmp_path / "dup.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"id": "x", "baseline_score": 0.9, "signal_source": "test-pass-rate"},
        {"id": "x", "baseline_score": 0.1, "signal_source": "test-pass-rate"},
    ]), encoding="utf-8")
    with pytest.raises(ValueError):
        ess._read_raw(p)


def test_score_map_before_after(tmp_path):
    cases = _corpus(tmp_path)
    before = ess.score_map(cases, phase="before")
    after = ess.score_map(cases, phase="after")
    # good case: after > before; regression: after < before
    assert after["good-fix"] > before["good-fix"]
    assert after["bad-regression"] < before["bad-regression"]
    # every case present in both maps
    assert set(before) == set(after) == {r["id"] for r in _ROWS}
