"""Trigger-vocabulary tests for zero-count-gate ( encode pass).

Pure predicate: _detect_trigger(claim_text) -> matched substring or None.
No I/O, no env reads, no subprocess — the module's only module-level
statements are two constant assignments and main() is guarded, so importing
it is side-effect free.

Covers the two compounds added after a MEASURED false negative: a
set-membership audit negation ("... are ABSENT from the blocklist -- 0 hits
each") returned trigger_matched=null, so Q2's automated half waved through
exactly the claim class this gate exists to catch. The regression block below
pins every pre-existing pattern, because the fix was additive and additive
changes to a matcher can only over-match — never under-match — so the
negative block is where a regression would actually surface.

Calls the PRODUCTION predicate rather than re-deriving the regex in the test
(guard-4323: a matcher rewrite has two halves, and validating against a
paraphrase of the application fails silently green).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
GATE = SCRIPTS_DIR / "zero-count-gate.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Hyphenated filename is not importable as a module name; load by path.
_spec = importlib.util.spec_from_file_location("zero_count_gate", GATE)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_detect_trigger = _mod._detect_trigger


# --- The measured miss, and the two compounds that close it ---------------

MEASURED_MISS = (
    "All six candidate brand terms (Vinheim, Pearl, zakbox1, zakpod1, zakbox, "
    "zakpod) are ABSENT from the 32-term domain-term blocklist -- 0 hits each "
    "-- and the domain-leak scanner reports CLEAN."
)


def test_measured_miss_now_triggers():
    """The exact claim that returned trigger_matched=null before the fix."""
    assert _detect_trigger(MEASURED_MISS) is not None


@pytest.mark.parametrize("claim,expected", [
    ("The scan returned 0 hits for every brand term.", "0 hits"),
    ("The scan returned zero hits for every brand term.", "zero hits"),
    ("There were no hits across the six terms.", "no hits"),
    ("The term zakpod1 is absent from the domain-term blocklist.", "is absent from"),
    ("All six terms are absent from the blocklist.", "are absent from"),
])
def test_new_compounds_match(claim, expected):
    assert _detect_trigger(claim) == expected


# --- Regression: every pre-existing pattern still fires -------------------

@pytest.mark.parametrize("claim", [
    "0 records have field domain_term set in the blocklist store",
    "98% of records have times_triggered=0 across the store",
    "the export is missing field owner_id for every row",
    "this is a zero-utilization claim about the entry",
    "all rows are zero in the sampled export",
    "12/40 records missing owner_id",
    "utilization_score is 0 across 113 records",
    "times_triggered=0 across the sampled window",
])
def test_preexisting_patterns_still_trigger(claim):
    assert _detect_trigger(claim) is not None


# --- Negative: the narrowing is real, not a bare-token add ---------------
#
# guard-1923 bars adding a bare common-English word ("absent") to a classifier
# vocabulary. These pin that the adjective only counts when it governs a
# source set, and that "hits" only counts under a zero-quantifier.

@pytest.mark.parametrize("claim", [
    "The reviewer was absent from the meeting on Tuesday.",   # past tense, not is/are
    "Several attendees were absent yesterday.",               # no from-clause
    "Cache hits improved by 12 percent after the change.",    # hits, no zero-quantifier
    "The endpoint took 40000 hits during the load test.",
    "Set the retry counter to zero before the run begins.",
    "The deployment succeeded and all tests passed on every box.",
    "The service is down and not responding to requests.",
])
def test_non_audit_prose_does_not_trigger(claim):
    assert _detect_trigger(claim) is None


def test_empty_and_none_are_inert():
    assert _detect_trigger("") is None
    assert _detect_trigger(None) is None
