"""Regression pins for predicate_fp_measure.py (gap-122 / ).

The pins that matter here are the REFUSALS, not the happy path. This tool exists
because a raw match count makes an unshippable predicate look fine, so every way
it could quietly emit a pass is a regression:

  - `sample` must NEVER emit a shippable verdict (it cannot compute the FP ratio).
  - an EMPTY corpus must refuse, never report a clean 0%.
  - a SAMPLED classification must carry the extrapolation caveat.
  - the goals adapter must span ALL statuses, not a convenient slice.
"""
import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "predicate_fp_measure", SCRIPTS / "predicate_fp_measure.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class Args:
    def __init__(self, **kw):
        self.predicate = kw.get("predicate", "x")
        self.corpus = kw.get("corpus", "files")
        self.path = kw.get("path")
        self.sample_size = kw.get("sample_size", 20)
        self.ignore_case = kw.get("ignore_case", False)


def _run(fn, args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(args)
    return rc, buf.getvalue()


# ---------------------------------------------------------------- refusals

def test_empty_corpus_refuses_rather_than_reporting_clean_zero(tmp_path):
    """The anti-vacuity floor. A rate over 0 units measures the loader."""
    rc, out = _run(mod.cmd_sample, Args(path=[str(tmp_path / "nothing-here-*")]))
    assert rc == 1
    d = json.loads(out)
    assert d["verdict"] == "refused"
    assert "empty corpus" in d["reason"]
    # must NOT have manufactured a rate
    assert "fire_rate" not in d


def test_sample_never_emits_a_shippable_verdict(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    rc, out = _run(mod.cmd_sample, Args(predicate="a", path=[str(f)]))
    assert rc == 0
    d = json.loads(out)
    assert d["verdict"] == "unclassified"
    assert "fp_ratio" not in d, "sample must not compute the FP ratio"
    assert "judgment call" in d["verdict_note"]


def test_score_refuses_empty_classification(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"classified": []}'))
    rc, _ = _run(mod.cmd_score, Args())
    assert rc == 1


def test_files_adapter_without_path_is_an_input_error():
    rc, _ = _run(mod.cmd_sample, Args(corpus="files", path=None))
    assert rc == 2


def test_bad_regex_exits_two(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _run(mod.cmd_sample, Args(predicate="[unclosed", path=[str(f)]))
    assert exc.value.code == 2


# ------------------------------------------------------------ measurement

def test_fire_rate_is_per_line_over_the_full_corpus(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hit\nmiss\nhit\nmiss\n", encoding="utf-8")
    rc, out = _run(mod.cmd_sample, Args(predicate="hit", path=[str(f)]))
    d = json.loads(out)
    assert rc == 0
    assert d["corpus_size"] == 4, "blank lines excluded, every non-blank line counted"
    assert d["match_count"] == 2
    assert d["fire_rate_pct"] == 50.0


def test_sample_is_bounded_and_flags_incompleteness(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("\n".join(f"hit{i}" for i in range(50)), encoding="utf-8")
    rc, out = _run(mod.cmd_sample, Args(predicate="hit", path=[str(f)], sample_size=5))
    d = json.loads(out)
    assert d["match_count"] == 50
    assert d["sample_size"] == 5
    assert d["sample_is_complete"] is False


def test_sample_complete_flag_is_true_when_all_matches_shown(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hit\nmiss\n", encoding="utf-8")
    _, out = _run(mod.cmd_sample, Args(predicate="hit", path=[str(f)], sample_size=20))
    assert json.loads(out)["sample_is_complete"] is True


# ----------------------------------------------------------------- scoring

def _score(payload, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return _run(mod.cmd_score, Args())


def test_majority_narration_is_unshippable(monkeypatch):
    rc, out = _score({"corpus_size": 100, "match_count": 3, "classified": [
        {"unit_id": "a", "verdict": "narration"},
        {"unit_id": "b", "verdict": "narration"},
        {"unit_id": "c", "verdict": "genuine"},
    ]}, monkeypatch)
    d = json.loads(out)
    assert rc == 0
    assert d["verdict"] == "unshippable"
    assert d["fp_pct"] == pytest.approx(66.67, abs=0.01)


def test_zero_fp_is_clean(monkeypatch):
    _, out = _score({"corpus_size": 100, "match_count": 2, "classified": [
        {"unit_id": "a", "verdict": "genuine"},
        {"unit_id": "b", "verdict": "genuine"},
    ]}, monkeypatch)
    assert json.loads(out)["verdict"] == "clean"


def test_any_narration_below_half_needs_narrowing(monkeypatch):
    _, out = _score({"corpus_size": 100, "match_count": 4, "classified": [
        {"unit_id": "a", "verdict": "genuine"},
        {"unit_id": "b", "verdict": "genuine"},
        {"unit_id": "c", "verdict": "genuine"},
        {"unit_id": "d", "verdict": "narration"},
    ]}, monkeypatch)
    assert json.loads(out)["verdict"] == "needs-narrowing"


def test_partial_classification_carries_the_extrapolation_caveat(monkeypatch):
    """A sample estimate quoted as a census is the defect one level up."""
    _, out = _score({"corpus_size": 6495, "match_count": 52, "classified": [
        {"unit_id": "a", "verdict": "genuine"},
    ]}, monkeypatch)
    d = json.loads(out)
    assert d["extrapolated_from_sample"] is True
    assert d["extrapolation_caveat"] and "not a census" in d["extrapolation_caveat"]


def test_full_classification_has_no_caveat(monkeypatch):
    _, out = _score({"corpus_size": 100, "match_count": 1, "classified": [
        {"unit_id": "a", "verdict": "genuine"},
    ]}, monkeypatch)
    d = json.loads(out)
    assert d["extrapolated_from_sample"] is False
    assert d["extrapolation_caveat"] is None


def test_unrecognised_verdict_counts_as_unclassifiable_not_as_pass(monkeypatch):
    """An unreadable label must not silently improve the ratio."""
    _, out = _score({"corpus_size": 10, "match_count": 2, "classified": [
        {"unit_id": "a", "verdict": "???"},
        {"unit_id": "b", "verdict": "narration"},
    ]}, monkeypatch)
    d = json.loads(out)
    assert d["unclassifiable"] == 1
    assert d["genuine"] == 0
    assert d["fp_pct"] == 50.0


def test_score_reports_both_metrics_because_one_cannot_answer_both(monkeypatch):
    _, out = _score({"corpus_size": 1000, "match_count": 10, "classified": [
        {"unit_id": "a", "verdict": "genuine"},
    ]}, monkeypatch)
    d = json.loads(out)
    assert d["fire_rate_pct"] == 1.0
    assert d["fp_pct"] == 0.0
    assert "different questions" in d["posture_note"]


# ------------------------------------------------------------ goal adapter

def test_goal_statuses_span_every_status_not_a_slice():
    """A pending-only corpus hides the completed majority where narration lives.

    Asserted against the AUTHORITATIVE set, not a hardcoded list. The first
    version of this test hardcoded the six statuses CLAUDE.md names and passed
    while the corpus silently omitted `decomposed` (16 live goals) and
    `superseded` — a superset assertion has zero discriminating power against
    the omission it exists to catch (guard-1836). Keyed to the real constant,
    it goes red the day a status is added and this adapter is not updated.
    """
    from aspirations import VALID_GOAL_STATUSES
    assert set(mod.GOAL_STATUSES) == set(VALID_GOAL_STATUSES)
    assert "decomposed" in mod.GOAL_STATUSES, "the status this test used to miss"


def test_partial_corpus_is_flagged_not_silently_measured(monkeypatch):
    """A survivors-only denominator must announce itself (guard-3068, rb-6245).

    Refusing would be wrong — one flaky status query would make the tool
    unusable — so the contract is that the failure is impossible to scroll
    past, not that the run aborts.
    """
    import json as _json
    calls = {"n": 0}

    def fake_run(cmd, timeout=180):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, _json.dumps([{"goal_id": "g-1", "title": "hit"}]), ""
        return 1, "", "daemon unreachable"

    monkeypatch.setattr(mod, "_run", fake_run)
    rc, out = _run(mod.cmd_sample, Args(predicate="hit", corpus="goals"))
    d = json.loads(out)
    assert rc == 0, "a partial corpus still measures — it must not abort"
    assert d["corpus_complete"] is False
    assert d["verdict_note"].startswith("PARTIAL CORPUS")
    assert len(d["corpus"]["failures"]) == len(mod.GOAL_STATUSES) - 1


def test_complete_corpus_is_marked_complete(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hit\n", encoding="utf-8")
    _, out = _run(mod.cmd_sample, Args(predicate="hit", path=[str(f)]))
    d = json.loads(out)
    assert d["corpus_complete"] is True
    assert not d["verdict_note"].startswith("PARTIAL CORPUS")


def test_goal_text_fields_include_description_and_outcome_note():
    """Title-only scanning makes any predicate read as near-zero-firing."""
    assert "description" in mod.GOAL_TEXT_FIELDS
    assert "outcome_note" in mod.GOAL_TEXT_FIELDS


def test_goal_unit_text_skips_missing_fields_without_crashing():
    assert mod.goal_unit_text({"title": "t"}) == "t"
    assert mod.goal_unit_text({}) == ""
    assert "d" in mod.goal_unit_text({"title": "t", "description": "d"})


# ------------------------------------------------- wrapper integration path
# Everything above calls cmd_sample/cmd_score as PYTHON FUNCTIONS and asserts
# on RETURN VALUES. The contract documented in the SKILL.md is PROCESS EXIT
# CODES reaching the caller through the bash wrapper's `exec python3`. That is
# a different claim, and nothing above tests it (sq-019 on ).
#
# This seam is not hypothetical: it is exactly where this goal's only real
# defect lived. The wrapper shipped without the cygpath -w conversion every
# sibling wrapper carries, and no test here noticed — it was caught only
# incidentally by test_cygpath_wrapper_pattern.py, whose population is a glob
# over core/scripts/*.sh that the new wrapper joined on creation.

from _bash_helpers import BASH  # noqa: E402  — resolves Git Bash on Windows

WRAPPER = SCRIPTS / "predicate-fp-measure.sh"


def _wrapper(*args, stdin=None):
    """Invoke the wrapper as a real process. STORAGE_BACKEND=local per guard-955."""
    import os
    import subprocess
    env = {**os.environ, "STORAGE_BACKEND": "local"}
    return subprocess.run(
        [BASH, str(WRAPPER), *args],
        capture_output=True, text=True, timeout=120, env=env,
        cwd=str(SCRIPTS.parent.parent), input=stdin,
    )


def test_wrapper_passes_through_exit_0_and_emits_json(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hit\nmiss\n", encoding="utf-8")
    p = _wrapper("sample", "--predicate", "hit", "--corpus", "files", "--path", str(f))
    assert p.returncode == 0, p.stderr
    d = json.loads(p.stdout)          # the JSON must survive `exec`, not just be returned
    assert d["verdict"] == "unclassified"
    assert d["match_count"] == 1


def test_wrapper_passes_through_exit_1_on_empty_corpus_refusal(tmp_path):
    """The refusal must reach the CALLER as rc=1, not just as a return value."""
    p = _wrapper("sample", "--predicate", "x", "--corpus", "files",
                 "--path", str(tmp_path / "nothing-here-*"))
    assert p.returncode == 1, p.stderr
    assert json.loads(p.stdout)["verdict"] == "refused"


def test_wrapper_passes_through_exit_2_on_input_error():
    p = _wrapper("sample", "--predicate", "x", "--corpus", "files")  # no --path
    assert p.returncode == 2, p.stderr


def test_wrapper_score_reads_stdin_through_exec():
    payload = json.dumps({"corpus_size": 10, "match_count": 2, "classified": [
        {"unit_id": "a", "verdict": "narration"},
        {"unit_id": "b", "verdict": "narration"},
    ]})
    p = _wrapper("score", stdin=payload)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["verdict"] == "unshippable"
