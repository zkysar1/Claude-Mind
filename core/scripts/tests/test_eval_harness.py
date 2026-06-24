"""Tests for eval_harness.py (Phase 0 keystone — the validation gate).

Hermetic: imports the pure module directly (no bound agent, no daemon, no
world/meta). conftest.py inserts core/scripts on sys.path.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import eval_harness as eh  # noqa: E402


# --------------------------------------------------------------------------- #
# Corpus model
# --------------------------------------------------------------------------- #


def _write(tmp_path, name, rows):
    p = tmp_path / name
    if name.endswith(".jsonl"):
        p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    else:
        p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def test_load_cases_jsonl_skips_comments_and_blanks(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('# header comment\n\n{"id": "a"}\n{"id": "b", "holdout": true}\n',
                 encoding="utf-8")
    cases = eh.load_cases(p)
    assert [c.id for c in cases] == ["a", "b"]
    assert cases[1].holdout is True
    assert cases[0].weight == 1.0


def test_load_cases_json_list(tmp_path):
    p = _write(tmp_path, "c.json", [{"id": "x", "weight": 2.0, "baseline_score": 0.5}])
    cases = eh.load_cases(p)
    assert cases[0].weight == 2.0 and cases[0].baseline_score == 0.5


def test_load_cases_empty_corpus_rejected(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("# only comments\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no cases"):
        eh.load_cases(p)


def test_load_cases_duplicate_ids_rejected(tmp_path):
    p = _write(tmp_path, "d.jsonl", [{"id": "a"}, {"id": "a"}])
    with pytest.raises(ValueError, match="duplicate"):
        eh.load_cases(p)


def test_case_missing_id_rejected(tmp_path):
    p = _write(tmp_path, "n.jsonl", [{"weight": 1.0}])
    with pytest.raises(ValueError, match="missing required 'id'"):
        eh.load_cases(p)


# --------------------------------------------------------------------------- #
# Scorers
# --------------------------------------------------------------------------- #


def test_scorers():
    assert eh.exact_match(" hi ", "hi") == 1.0
    assert eh.exact_match("hi", "bye") == 0.0
    assert eh.numeric_closeness(10, 10, 5) == 1.0
    assert eh.numeric_closeness(12.5, 10, 5) == pytest.approx(0.5)
    assert eh.numeric_closeness(100, 10, 5) == 0.0  # clamped, not negative
    assert eh.contains_all("the QUICK brown fox", ["quick", "fox"]) == 1.0
    assert eh.contains_all("only quick", ["quick", "fox"]) == pytest.approx(0.5)
    assert eh.contains_all("anything", []) == 1.0
    assert eh.clamp01(-3) == 0.0 and eh.clamp01(3) == 1.0


def test_numeric_closeness_rejects_nonpositive_tolerance():
    with pytest.raises(ValueError):
        eh.numeric_closeness(1, 1, 0)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _cases():
    return [
        eh.EvalCase(id="t1", weight=1.0, holdout=False),
        eh.EvalCase(id="t2", weight=3.0, holdout=False),   # heavier
        eh.EvalCase(id="h1", weight=1.0, holdout=True),
        eh.EvalCase(id="h2", weight=1.0, holdout=True),
    ]


def test_aggregate_weighted_mean():
    cases = _cases()
    scores = {"t1": 1.0, "t2": 0.0, "h1": 0.5, "h2": 0.5}
    # train split: (1*1 + 0*3) / (1+3) = 0.25
    assert eh.aggregate(scores, cases, "train") == pytest.approx(0.25)
    # holdout split: (0.5 + 0.5) / 2 = 0.5
    assert eh.aggregate(scores, cases, "holdout") == pytest.approx(0.5)
    # all: (1 + 0 + 0.5 + 0.5) / 6 = 0.3333
    assert eh.aggregate(scores, cases, "all") == pytest.approx(2.0 / 6)


def test_aggregate_clamps_out_of_range_scores():
    cases = [eh.EvalCase(id="a", holdout=True)]
    assert eh.aggregate({"a": 5.0}, cases, "holdout") == 1.0
    assert eh.aggregate({"a": -5.0}, cases, "holdout") == 0.0


def test_aggregate_missing_score_fails_loud():
    cases = _cases()
    with pytest.raises(ValueError, match="missing"):
        eh.aggregate({"t1": 1.0}, cases, "train")  # t2 missing


def test_aggregate_empty_split_fails_loud():
    cases = [eh.EvalCase(id="t1", holdout=False)]  # no holdout cases
    with pytest.raises(ValueError, match="no cases in split"):
        eh.aggregate({"t1": 1.0}, cases, "holdout")


# --------------------------------------------------------------------------- #
# The gate — "earn the keep"
# --------------------------------------------------------------------------- #


def test_gate_no_regression_passes_on_equal():
    cases = _cases()
    before = {"h1": 0.5, "h2": 0.5}
    after = {"h1": 0.5, "h2": 0.5}
    v = eh.gate(before, after, cases, policy="no_regression")
    assert v.passed is True and v.delta == 0.0 and v.split == "holdout" and v.n_cases == 2


def test_gate_no_regression_fails_on_drop():
    cases = _cases()
    before = {"h1": 0.8, "h2": 0.8}
    after = {"h1": 0.4, "h2": 0.4}
    v = eh.gate(before, after, cases, policy="no_regression")
    assert v.passed is False and v.delta == pytest.approx(-0.4)


def test_gate_no_regression_epsilon_dead_band():
    cases = _cases()
    before = {"h1": 0.8, "h2": 0.8}
    after = {"h1": 0.78, "h2": 0.78}  # -0.02 drop
    assert eh.gate(before, after, cases, "no_regression", epsilon=0.05).passed is True
    assert eh.gate(before, after, cases, "no_regression", epsilon=0.0).passed is False


def test_gate_strict_improve_requires_real_gain():
    cases = _cases()
    before = {"h1": 0.5, "h2": 0.5}
    equal = {"h1": 0.5, "h2": 0.5}
    better = {"h1": 0.9, "h2": 0.9}
    assert eh.gate(before, equal, cases, "strict_improve").passed is False
    assert eh.gate(before, better, cases, "strict_improve").passed is True


def test_gate_evaluates_holdout_not_train_by_default():
    # Improve train massively but regress holdout -> gate must FAIL (can't tune to test).
    cases = _cases()
    before = {"t1": 0.1, "t2": 0.1, "h1": 0.9, "h2": 0.9}
    after = {"t1": 1.0, "t2": 1.0, "h1": 0.2, "h2": 0.2}
    v = eh.gate(before, after, cases, policy="no_regression")  # split defaults to holdout
    assert v.passed is False
    assert v.before == pytest.approx(0.9) and v.after == pytest.approx(0.2)


def test_gate_unknown_policy_rejected():
    with pytest.raises(ValueError):
        eh.gate({}, {}, _cases(), policy="bogus")


def test_verdict_as_dict_round_trips():
    v = eh.gate({"h1": 0.5, "h2": 0.5}, {"h1": 0.6, "h2": 0.6}, _cases(),
                policy="strict_improve")
    d = v.as_dict()
    assert set(d) == {"passed", "before", "after", "delta", "policy", "split",
                      "n_cases", "reason"}
    assert d["passed"] is True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Regression tests for fresh-eyes review findings (2026-06-13)
# --------------------------------------------------------------------------- #


def test_nan_score_fails_loud_not_silent_verdict():
    # HIGH finding: NaN silently poisoned the verdict (nan<0 and nan>1 both False).
    cases = [eh.EvalCase(id="h1", holdout=True), eh.EvalCase(id="h2", holdout=True)]
    nan = float("nan")
    with pytest.raises(ValueError, match="finite"):
        eh.aggregate({"h1": nan, "h2": 0.5}, cases, "holdout")
    with pytest.raises(ValueError, match="finite"):
        eh.gate({"h1": 0.8, "h2": 0.8}, {"h1": nan, "h2": nan}, cases, "no_regression")


def test_inf_score_fails_loud():
    with pytest.raises(ValueError, match="finite"):
        eh.clamp01(float("inf"))


def test_negative_weight_rejected_at_construction():
    # HIGH/MED finding: negative weight could push aggregate outside [0,1] and invert the gate.
    with pytest.raises(ValueError, match="non-negative"):
        eh.EvalCase.from_dict({"id": "a", "weight": -1.0})


def test_unknown_case_id_in_scores_fails_loud():
    # MED finding: a typo'd id silently dropped the real case's score.
    cases = [eh.EvalCase(id="h1", holdout=True), eh.EvalCase(id="h2", holdout=True)]
    with pytest.raises(ValueError, match="unknown case ids"):
        eh.aggregate({"h1": 0.9, "h2": 0.9, "h3_TYPO": 0.0}, cases, "holdout")


def test_load_scores_rejects_null_and_bool(tmp_path):
    # MED finding: JSON null crashed opaquely; bool silently coerced to 1.0/0.0.
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"a": None}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a number"):
        eh._load_scores(p)
    p.write_text(json.dumps({"a": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a number"):
        eh._load_scores(p)


def test_cli_gate_exit_codes(tmp_path, capsys):
    cases_p = _write(tmp_path, "c.jsonl",
                     [{"id": "h1", "holdout": True}, {"id": "h2", "holdout": True}])
    before_p = tmp_path / "before.json"
    before_p.write_text(json.dumps({"h1": 0.5, "h2": 0.5}), encoding="utf-8")
    pass_p = tmp_path / "pass.json"
    pass_p.write_text(json.dumps({"h1": 0.9, "h2": 0.9}), encoding="utf-8")
    fail_p = tmp_path / "fail.json"
    fail_p.write_text(json.dumps({"h1": 0.1, "h2": 0.1}), encoding="utf-8")

    rc = eh.main(["gate", "--cases", str(cases_p), "--before", str(before_p),
                  "--after", str(pass_p), "--policy", "strict_improve"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["passed"] is True

    rc = eh.main(["gate", "--cases", str(cases_p), "--before", str(before_p),
                  "--after", str(fail_p), "--policy", "no_regression"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["passed"] is False
