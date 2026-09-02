"""Tests for core/scripts/scan-cross-pollination.py (S4b detector, ).

Two halves, deliberately:
  - the PURE CORE (`scan`) against synthetic corpora, including the positive
    control that both branches are reachable;
  - the WIRING, pinning that aspirations-strategic-scan actually CALLS the
    script. The predecessor defect was a detector that ran and told you
    nothing; a detector nothing calls is the same defect one layer out
    (guard-1943: pinning the writer says nothing about the wiring).
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SCRIPT = CORE_SCRIPTS / "scan-cross-pollination.py"
SKILL = (PROJECT_ROOT / ".claude" / "skills"
         / "aspirations-strategic-scan" / "SKILL.md")


def _load():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("scan_cross_pollination", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scp = _load()


def _rec(rec_id, retrieval, v2):
    return {"id": rec_id,
            "utilization": {"retrieval_count": retrieval, "utilization_score_v2": v2}}


# ---------------------------------------------------------------- pure core

def test_qualifying_entry_is_surfaced():
    out = scp.scan([_rec("rb-1", 159, 0.0078)])
    assert out["top"]["id"] == "rb-1"
    assert out["top"]["retrieval_count"] == 159
    assert out["candidates"] == 1


def test_young_entry_is_excluded_by_the_maturity_floor():
    """The whole point of MATURITY: a 0.0 score on a never-retrieved entry
    means 'too new to have been used', not 'not paying off'."""
    out = scp.scan([_rec("rb-new", 0, 0.0)])
    assert out["mature"] == 0
    assert out["top"] is None
    # ... and `scanned` still reports it, so the zero is interpretable.
    assert out["scanned"] == 1


def test_well_used_entry_is_excluded_by_the_ceiling():
    out = scp.scan([_rec("rb-good", 50, 0.9)])
    assert out["mature"] == 1          # it HAD the opportunity
    assert out["candidates"] == 0      # it simply does not qualify
    assert out["top"] is None


def test_both_branches_are_reachable_on_one_corpus():
    """Positive control against the predecessor defect, which admitted 100%.
    A detector that cannot say 'no' is not a detector (guard-1665)."""
    corpus = [_rec("rb-fires", 40, 0.01), _rec("rb-holds", 40, 0.80)]
    out = scp.scan(corpus)
    assert out["mature"] == 2
    assert out["candidates"] == 1
    assert out["top"]["id"] == "rb-fires"


def test_lowest_score_wins_then_highest_retrieval_count():
    corpus = [_rec("rb-b", 10, 0.02), _rec("rb-a", 99, 0.01), _rec("rb-c", 5, 0.01)]
    out = scp.scan(corpus)
    # 0.01 beats 0.02; among the 0.01s, the more-retrieved one is the stronger
    # transfer candidate (it keeps surfacing and keeps not paying off).
    assert out["top"]["id"] == "rb-a"


def test_scanned_is_the_unfiltered_population():
    """guard-2298: `candidates: 0` is only interpretable beside the population
    it was filtered from. Zero-of-zero and zero-of-many are different claims."""
    empty = scp.scan([])
    many = scp.scan([_rec("rb-x", 40, 0.9)] * 7)
    assert empty["scanned"] == 0 and empty["top"] is None
    assert many["scanned"] == 7 and many["top"] is None


def test_missing_utilization_block_does_not_crash():
    out = scp.scan([{"id": "rb-bare"}])
    assert out["scanned"] == 1 and out["mature"] == 0


@pytest.mark.parametrize("maturity,expected", [(1, 1), (3, 1), (11, 0)])
def test_maturity_is_honoured_as_passed(maturity, expected):
    out = scp.scan([_rec("rb-1", 10, 0.0)], maturity=maturity)
    assert out["candidates"] == expected


# ------------------------------------------------------------------ wiring

def test_skill_invokes_the_script():
    """The detector must be REACHED. Prose describing it is not a call site."""
    text = SKILL.read_text(encoding="utf-8")
    assert re.search(r"^Bash: py -3 core/scripts/scan-cross-pollination\.py --category",
                     text, re.M), "aspirations-strategic-scan no longer calls the S4b detector"


def test_skill_no_longer_uses_the_recency_sample_for_s4b():
    """The recalibration's load-bearing half was the SAMPLE. If `--recent`
    comes back for S4b, the 100%-admit defect is back with it."""
    text = SKILL.read_text(encoding="utf-8")
    s4b = text[text.index("# S4b: Cross-pollination"):]
    s4b = s4b[:s4b.index("## Phase S4.5")]
    assert "--recent" not in s4b
    assert "times_helpful" not in s4b


def test_rationale_file_is_present_for_the_pointer_in_the_skill():
    """The SKILL.md carries a `Rationale (...): <path>` pointer instead of the
    measurement narrative; a dangling pointer loses the whole justification."""
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(r"# Rationale \([^)]*\): (core/config/rationale/\S+\.md)", text)
    assert m, "S4b rationale pointer missing from the skill"
    assert (PROJECT_ROOT / m.group(1)).is_file(), f"dangling pointer: {m.group(1)}"
