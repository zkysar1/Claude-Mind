"""Tests for _actions_budget_classify ().

The load-bearing cases are the two that make the LOUD alert trustworthy:
an all-skipped run must NOT read as budget-exhausted (vacuous-truth trap,
guard-1715), and an annotation string must never outvote executed steps
(guard-1265).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _actions_budget_classify import classify_run, BUDGET_ANNOTATION

ANN = "The job was not started because an Actions budget is preventing further use."


def _job(conclusion="failure", steps=0, ann=False):
    return {
        "conclusion": conclusion,
        "steps": [{"number": i} for i in range(steps)],
        "annotations": [ANN] if ann else [],
    }


def test_budget_signature_all_zero_step_with_annotation():
    r = classify_run({"conclusion": "failure", "jobs": [_job(ann=True), _job(ann=True)]})
    assert r["verdict"] == "budget_exhausted"
    assert r["non_skipped"] == 2 and r["zero_step"] == 2
    assert r["annotation_hits"] == 2
    assert not r["annotation_only"]


def test_structural_signal_alone_still_decides_budget():
    """No annotation (GitHub changed the wording, or it was not fetched) —
    all-zero-step is sufficient. The annotation corroborates; it is not required."""
    r = classify_run({"conclusion": "failure", "jobs": [_job(), _job()]})
    assert r["verdict"] == "budget_exhausted"
    assert r["annotation_hits"] == 0
    assert "structural signal decides" in r["reason"]


def test_real_failure_when_any_job_executed_steps():
    r = classify_run({"conclusion": "failure", "jobs": [_job(steps=5), _job()]})
    assert r["verdict"] == "real_failure"
    assert r["zero_step"] == 1 and r["non_skipped"] == 2


def test_annotation_never_outvotes_executed_steps():
    """guard-1265: a string match must not decide against structural evidence.
    A real failure carrying a stale/incidental budget annotation stays real,
    and the disagreement is reported rather than swallowed."""
    r = classify_run({"conclusion": "failure",
                      "jobs": [_job(steps=3, ann=True), _job(steps=1)]})
    assert r["verdict"] == "real_failure"
    assert r["annotation_only"] is True
    assert "CONTRADICTED" in r["reason"]


def test_all_skipped_is_indeterminate_not_budget():
    """guard-1715 / the vacuous-truth trap: 'every non-skipped job has steps==0'
    is TRUE over an empty set. If that returned budget_exhausted, an all-skipped
    run would fire the loud alert this detector exists to make trustworthy."""
    r = classify_run({"conclusion": "failure",
                      "jobs": [_job(conclusion="skipped"), _job(conclusion="skipped")]})
    assert r["verdict"] == "indeterminate"
    assert r["non_skipped"] == 0
    assert "vacuous" in r["reason"]


def test_empty_job_list_is_indeterminate_not_budget():
    """Same trap by a different route: the jobs list failed to load."""
    r = classify_run({"conclusion": "failure", "jobs": []})
    assert r["verdict"] == "indeterminate"
    assert r["non_skipped"] == 0


def test_all_skipped_with_annotation_is_still_not_budget():
    """The strongest form of the trap: annotation present, zero executed-step
    evidence. Must stay indeterminate and flag annotation_only."""
    r = classify_run({"conclusion": "failure",
                      "jobs": [_job(conclusion="skipped", ann=True)]})
    assert r["verdict"] == "indeterminate"
    assert r["annotation_only"] is True


def test_non_failure_conclusions_are_not_judged():
    for c in ("success", "cancelled", "", None):
        r = classify_run({"conclusion": c, "jobs": [_job()]})
        assert r["verdict"] == "not_a_failure"


def test_skipped_jobs_excluded_from_population():
    """A budget run where some jobs were skipped: the skipped ones must not
    dilute or inflate the count."""
    r = classify_run({"conclusion": "failure",
                      "jobs": [_job(), _job(conclusion="skipped"), _job()]})
    assert r["verdict"] == "budget_exhausted"
    assert r["non_skipped"] == 2


def test_annotation_match_is_case_insensitive_and_substring():
    r = classify_run({"conclusion": "failure",
                      "jobs": [{"conclusion": "failure", "steps": [],
                                "annotations": ["An Actions BUDGET IS PREVENTING FURTHER USE right now"]}]})
    assert r["annotation_hits"] == 1
    assert BUDGET_ANNOTATION == "budget is preventing further use"


def test_malformed_job_fields_do_not_crash():
    r = classify_run({"conclusion": "failure",
                      "jobs": [{"conclusion": None, "steps": None, "annotations": None}]})
    assert r["verdict"] == "budget_exhausted"
    assert r["non_skipped"] == 1
