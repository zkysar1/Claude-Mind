"""Tests for core/scripts/batch_corpus_rate.py (gap-101, ).

WHAT THIS GUARDS. The tool exists to stop a selection artifact being encoded as
a finding, and its whole value is a verdict a caller trusts without re-deriving
it. Three properties are load-bearing and none is visible by reading the JSON:

  1. PRECEDENCE. The outcome control decides. A surviving confounder control is
     NOT clearance to encode. Built from the earlier encounters alone the tool
     would have implemented the length-match and stopped, and it would have
     PASSED the measured case incorrectly.
  2. THE SEPARATION IS REPORTED EVEN AT ~0. A 0.1pp separation is the finding --
     a shape that is real, common, and wholly unrelated to being wrong -- not an
     absence of one.
  3. A MISSING FIELD IS AN ERROR, NEVER A ZERO. A reader that mismatches its
     input's shape returns empty, and empty is a well-formed, actionable-looking
     answer (guard-2421 / rb-245).

THE TWO WORKED EXAMPLES ARE REPLAYED AS FIXTURES, with the measured numbers
asserted, so the thresholds have a regression anchor rather than a remembered
one. Both come from g-115-5237's addendum (alpha, cc-04, 2026-08-07).

Run: STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_batch_corpus_rate.py -q
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "batch_corpus_rate.py"
_spec = importlib.util.spec_from_file_location("batch_corpus_rate", _SRC)
bcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bcr)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _spec_for(batch, corpus, confounder="len"):
    s = {"batch": batch, "corpus": corpus, "indicator_field": "hit",
         "outcome_field": "outcome", "positive_outcome": "CORRECTED",
         "negative_outcome": "CONFIRMED"}
    if confounder:
        s["confounder_field"] = confounder
    return s


def _mechanism_misattribution():
    """WORKED EXAMPLE 1 — the case that established the precedence rule.

    Measured: batch 30.0% (3/10) vs corpus 4.2% (11/261) = 7.1x. Length-matching
    did NOT collapse it and 18% of corpus hits fell below the batch minimum, so
    the confounder control says KEEP. The outcome control overruled it at +3.0pp,
    under the 10pp bar. Verdict BATCH-SCOPED; nothing was encoded.
    """
    batch = [{"hit": i < 3, "len": 800 + i * 30, "outcome": "CORRECTED"}
             for i in range(10)]
    corpus = []
    for i in range(261):
        hit = i < 11
        # 2 of the 11 hits (18.2%) sit below the batch minimum of 800.
        corpus.append({"hit": hit, "len": 400 if (hit and i < 2) else 900,
                       "outcome": "CORRECTED" if i % 4 == 0 else "CONFIRMED"})
    return _spec_for(batch, corpus)


def _flat_separation():
    """WORKED EXAMPLE 2 — instrument-defect shape: 40% in-batch, and CORRECTED
    12.5% vs CONFIRMED 12.6% at corpus scale. A 0.1pp separation."""
    batch = [{"hit": i < 4, "len": 900, "outcome": "CORRECTED"} for i in range(10)]
    corpus = ([{"hit": i < 20, "len": 900, "outcome": "CORRECTED"} for i in range(160)]
              + [{"hit": i < 20, "len": 900, "outcome": "CONFIRMED"} for i in range(159)])
    return _spec_for(batch, corpus)


# ---------------------------------------------------------------------------
# The measured worked examples
# ---------------------------------------------------------------------------
def test_worked_example_reproduces_the_measured_numbers():
    r = bcr.compare(_mechanism_misattribution())
    assert r["batch_rate_pct"] == 30.0
    assert r["corpus_rate_pct"] == pytest.approx(4.21, abs=0.02)
    assert r["unmatched_ratio"] == pytest.approx(7.1, abs=0.05), \
        "the measured 7.1x multiple is the regression anchor"


def test_the_outcome_control_OVERRULES_a_surviving_confounder_control():
    """THE precedence property. Built from the earlier encounters alone the tool
    would have stopped at the length-match and passed this case incorrectly."""
    r = bcr.compare(_mechanism_misattribution())
    assert r["confounder_control"]["verdict"] == "KEEP"
    assert r["outcome_control"]["verdict"] == "BATCH-SCOPED"
    assert r["verdict"] == "BATCH-SCOPED", \
        "a surviving confounder control is NOT clearance to encode"
    assert r["decided_by"] == "outcome_control"


def test_a_disagreement_between_the_controls_is_reported_not_hidden():
    r = bcr.compare(_mechanism_misattribution())
    assert r["verdicts_conflict"] is True
    assert "DISAGREE" in r["conflict_note"]
    # Both verdicts survive separately so a caller can see WHICH arm objected.
    assert r["confounder_control"]["verdict"] != r["outcome_control"]["verdict"]


def test_below_minimum_fraction_matches_the_measured_18_percent():
    r = bcr.compare(_mechanism_misattribution())
    bm = r["confounder_control"]["below_minimum"]
    assert bm["corpus_hits_considered"] == 11
    assert bm["below_batch_min"] == 2
    assert bm["fraction"] == pytest.approx(0.182, abs=0.005)


def test_a_near_zero_separation_is_REPORTED_not_treated_as_absent():
    """guard-2273 from the other side: a shape that is real, common, and wholly
    unrelated to being wrong. Only the outcome arm surfaces it."""
    r = bcr.compare(_flat_separation())
    sep = r["outcome_control"]["separation_pp"]
    assert sep is not None, "a ~0 separation must be a NUMBER, never omitted"
    assert abs(sep) < 1.0, "measured 0.1pp"
    assert r["outcome_control"]["verdict"] == "BATCH-SCOPED"
    assert r["verdict"] == "BATCH-SCOPED"


# ---------------------------------------------------------------------------
# Precedence in the OTHER direction (the half a one-sided test would miss)
# ---------------------------------------------------------------------------
def test_a_CONFOUNDED_length_match_does_not_veto_a_separating_indicator():
    """Precedence must hold both ways, or it is just 'the pessimistic arm wins'.

    Here the indicator only ever fires on long records (confounder control says
    CONFOUNDED) but it separates outcomes by well over the bar. The outcome
    control decides, so the verdict is KEEP.
    """
    batch = [{"hit": i < 5, "len": 1000, "outcome": "CORRECTED"} for i in range(10)]
    corpus = ([{"hit": True, "len": 1000, "outcome": "CORRECTED"} for _ in range(60)]
              + [{"hit": False, "len": 1000, "outcome": "CORRECTED"} for _ in range(40)]
              + [{"hit": False, "len": 1000, "outcome": "CONFIRMED"} for _ in range(100)])
    r = bcr.compare(_spec_for(batch, corpus))
    assert r["confounder_control"]["verdict"] == "CONFOUNDED"
    assert r["outcome_control"]["verdict"] == "KEEP"
    assert r["verdict"] == "KEEP", "the outcome control decides in BOTH directions"
    assert r["verdicts_conflict"] is True


def test_agreement_sets_no_conflict_flag():
    r = bcr.compare(_flat_separation())
    # both arms land on the non-KEEP side here
    assert r["verdicts_conflict"] is False


# ---------------------------------------------------------------------------
# A missing field is an ERROR, never an implicit zero (guard-2421 / rb-245)
# ---------------------------------------------------------------------------
def test_an_absent_indicator_field_raises_rather_than_reporting_zero():
    batch = [{"outcome": "CORRECTED"} for _ in range(5)]
    corpus = [{"outcome": "CONFIRMED"} for _ in range(5)]
    with pytest.raises(KeyError) as e:
        bcr.compare(_spec_for(batch, corpus, confounder=None))
    assert "absent from ALL" in str(e.value)


def test_a_typo_in_the_indicator_field_does_not_silently_produce_zero_percent(tmp_path):
    """The whole failure mode in one test: a wrong field name yields 0%, and 0%
    is a well-formed answer nobody questions."""
    spec = _mechanism_misattribution()
    spec["indicator_field"] = "hitt"          # one keystroke
    p = tmp_path / "in.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    rc = bcr.main(["--input", str(p), "--json"])
    assert rc == 2, "must exit 2, NOT 0 with a confident 0% rate"


def test_empty_batch_or_corpus_is_refused():
    with pytest.raises(ValueError):
        bcr.compare(_spec_for([], [{"hit": True, "outcome": "CORRECTED"}], None))
    with pytest.raises(ValueError):
        bcr.compare(_spec_for([{"hit": True, "outcome": "CORRECTED"}], [], None))


# ---------------------------------------------------------------------------
# Honesty of the output contract
# ---------------------------------------------------------------------------
def test_every_ratio_carries_the_formula_that_produced_it():
    """guard-2573: two defensible formulas over one quantity can invert a trend,
    and each number is individually correct so there is nothing to notice."""
    r = bcr.compare(_mechanism_misattribution())
    assert "formula" in r["unmatched_formula"] or r["unmatched_formula"]
    cc = r["confounder_control"]
    assert cc["unmatched"]["formula"]
    assert cc["unmatched"]["bases"]["batch_n"] == 10
    for cell in cc["matched"].values():
        assert cell["formula"], "a matched ratio without its threshold is unreadable"
    assert cc["below_minimum"]["formula"]
    assert r["outcome_control"]["formula"]


def test_the_output_names_what_it_does_NOT_check():
    """guard-1462: a verdict that does not declare its excluded layers reads as
    total coverage. Date/lane collapse is the caller's job (guard-2144)."""
    r = bcr.compare(_mechanism_misattribution())
    assert "date/lane" in r["not_covered"]
    assert "guard-2144" in r["not_covered"]


def test_skipping_the_confounder_arm_says_so_rather_than_passing_silently():
    batch = [{"hit": i < 3, "outcome": "CORRECTED"} for i in range(10)]
    corpus = [{"hit": i < 5, "outcome": "CONFIRMED"} for i in range(100)]
    r = bcr.compare(_spec_for(batch, corpus, confounder=None))
    assert r["confounder_control"]["verdict"] == "NOT-RUN"
    assert "not the same as it passing" in r["confounder_control"]["reason"]


def test_matched_cells_report_n_and_flag_a_thin_one():
    batch = [{"hit": i < 3, "len": 5000, "outcome": "CORRECTED"} for i in range(10)]
    corpus = ([{"hit": True, "len": 5000, "outcome": "CORRECTED"} for _ in range(3)]
              + [{"hit": False, "len": 10, "outcome": "CONFIRMED"} for _ in range(200)])
    r = bcr.compare(_spec_for(batch, corpus))
    cell = r["confounder_control"]["matched"]["at_batch_min"]
    assert cell["matched_n"] == 3
    assert cell["thin"] is True, "a 3-record matched cell must not carry a verdict silently"


# ---------------------------------------------------------------------------
# Arithmetic edges that would otherwise fabricate a number
# ---------------------------------------------------------------------------
def test_a_zero_corpus_rate_yields_None_not_zero_or_infinity():
    batch = [{"hit": True, "outcome": "CORRECTED"} for _ in range(5)]
    corpus = [{"hit": False, "outcome": "CONFIRMED"} for _ in range(50)]
    r = bcr.compare(_spec_for(batch, corpus, confounder=None))
    assert r["unmatched_ratio"] is None, \
        "a made-up ratio would propagate into the verdict as if measured"


def test_an_empty_outcome_arm_is_insufficient_data_not_a_verdict():
    batch = [{"hit": True, "outcome": "CORRECTED"} for _ in range(5)]
    corpus = [{"hit": True, "outcome": "CORRECTED"} for _ in range(50)]  # no CONFIRMED
    r = bcr.compare(_spec_for(batch, corpus, confounder=None))
    assert r["outcome_control"]["verdict"] == "INSUFFICIENT-DATA"
    assert r["outcome_control"]["separation_pp"] is None
    assert r["verdict"] == "INSUFFICIENT-DATA"


@pytest.mark.parametrize("val,expected", [
    (True, True), (1, True), ("yes", True), ("CORRECTED", True),
    (False, False), (0, False), ("", False), ("false", False),
    ("False", False), ("0", False), ("no", False), (None, False),
])
def test_string_falsey_indicators_are_not_counted_as_hits(val, expected):
    """A JSON export writing "false" as a STRING would otherwise read as a hit,
    inflating every rate in the report."""
    assert bcr._truthy(val) is expected


# ---------------------------------------------------------------------------
# Exit contract
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("builder,expected_rc", [
    (_mechanism_misattribution, 1),   # BATCH-SCOPED
    (_flat_separation, 1),            # BATCH-SCOPED
])
def test_exit_1_only_for_batch_scoped(tmp_path, builder, expected_rc):
    p = tmp_path / "in.json"
    p.write_text(json.dumps(builder()), encoding="utf-8")
    assert bcr.main(["--input", str(p), "--json"]) == expected_rc


def test_a_keep_verdict_exits_zero(tmp_path):
    batch = [{"hit": i < 5, "len": 1000, "outcome": "CORRECTED"} for i in range(10)]
    corpus = ([{"hit": True, "len": 1000, "outcome": "CORRECTED"} for _ in range(60)]
              + [{"hit": False, "len": 1000, "outcome": "CORRECTED"} for _ in range(40)]
              + [{"hit": False, "len": 1000, "outcome": "CONFIRMED"} for _ in range(100)])
    p = tmp_path / "in.json"
    p.write_text(json.dumps(_spec_for(batch, corpus)), encoding="utf-8")
    assert bcr.main(["--input", str(p), "--json"]) == 0


def test_an_unreadable_input_exits_2(tmp_path):
    assert bcr.main(["--input", str(tmp_path / "nope.json"), "--json"]) == 2
