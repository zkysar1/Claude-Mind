"""test_target_state_check_positional.py — `check` as final prefix word.

Background (g-248-88, 2026-05-17):
  Prior cycle (g-248-81, 2026-05-10) excluded `check` from READ_INTENT_VERBS
  due to ambiguity between read-intent ("Strategic vision check") and
  write-intent ("Added verify-learning check"). g-248-28 classifier scan on
  2026-05-17 found 15 false-negatives on `check` over the audit window;
  disambiguating pattern: when `check` is the LAST word of the title prefix
  it consistently indicates read-intent.

The positional rule fires BEFORE the frozenset loop in is_read_intent().
This test pins the contract: read-intent TPs when `check` is final-prefix-word,
no false-positives on common write-intent phrases that mention `check`
mid-sentence or as a fragment.

Cross-refs: rb-648 (verify named hook target), g-248-28 (FN measurement).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# _target_state.py has an underscore prefix but is otherwise an ordinary
# module name; importlib over spec_from_file_location avoids any ambiguity
# with packaging / __init__ side-effects.
TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

is_read_intent = ts_mod.is_read_intent


# ─── Positive cases: `check` as final prefix word → True ────────────────

def test_strategic_vision_check_colon():
    """Canonical read-intent example from goal description ()."""
    assert is_read_intent("Strategic vision check:") is True


def test_pipeline_health_check_colon():
    """Recurring health-check goals are read-intent."""
    assert is_read_intent("Pipeline health check:") is True


def test_strategic_vision_check_no_colon():
    """Whole title becomes prefix when no colon present."""
    assert is_read_intent("Strategic vision check") is True


def test_single_word_check_colon():
    """Minimal case: 'check:' alone."""
    assert is_read_intent("check:") is True


def test_check_with_possessive_apostrophe_s():
    """Possessive 's stripped before comparison (rare but covered by code)."""
    assert is_read_intent("Alpha's check:") is True


def test_capitalized_check_final():
    """Case-insensitive match — Check, CHECK should all qualify."""
    assert is_read_intent("System Check:") is True
    assert is_read_intent("System CHECK:") is True


def test_multi_word_prefix_check_final():
    """Multi-word prefix where `check` is the last word."""
    assert is_read_intent("Cross agent context bleed check:") is True


# ─── Negative cases: `check` NOT final prefix word → False ──────────────

def test_check_mid_sentence_write_intent():
    """Goal description evidence: 'Added _check_partner_in_flight' is write-intent."""
    assert is_read_intent("Added _check_partner_in_flight to wrapper") is False


def test_check_followed_by_more_words():
    """`check pair` — `check` is not final word, no match."""
    assert is_read_intent("verify-learning check pair") is False


def test_check_in_compound_phrase():
    """`check additions` — `check` is not final word."""
    assert is_read_intent("verify-learning check additions") is False


def test_check_substring_of_other_word():
    """Substring `check` inside `checker` must not match."""
    assert is_read_intent("Refactor checker to use new API") is False


def test_check_underscored_identifier():
    """`_check_X` identifier mid-title must not match (not the same token)."""
    assert is_read_intent("Wire _check_token into pipeline") is False


def test_empty_title():
    """Empty/None title short-circuit unchanged."""
    assert is_read_intent("") is False
    assert is_read_intent(None) is False


# ─── Regression cases: existing READ_INTENT_VERBS still work ────────────
# Guard against the positional rule accidentally breaking frozenset matches.

def test_investigate_unchanged():
    assert is_read_intent("Investigate: foo bar") is True


def test_audit_unchanged():
    assert is_read_intent("Audit: schema drift") is True


def test_probe_unchanged():
    assert is_read_intent("Probe: pipeline latency") is True


def test_scan_unchanged():
    assert is_read_intent("Scan: cross-agent overlap") is True
