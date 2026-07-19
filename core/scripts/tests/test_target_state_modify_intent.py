"""test_target_state_modify_intent.py — MODIFY_INTENT_VERBS carve-out.

Background (g-115-2565, echo g-115-2556, 2026-07-18):
  Modify goals (fix / extend / wire / refactor / ...) NAME the existing
  symbols they change — those identifiers are the modification SUBJECT,
  present in the target file BOTH before and after the work, not a completion
  signal. echo's g-115-2556 quantification: 71% (37/52) of target_state
  SOLO-block overrides since 2026-07-04 were subject-not-deliverable modify-verb
  FPs (citation-shape 16, modification-surface 11, test-absence 6,
  union-masks-miss 4).

CRITICAL DIFFERENCE from read/removal intent — the caller DEMOTES, not skips.
  Read/removal presence INVERTS the completion semantic (presence == NOT done),
  so a full skip is safe. Modify-presence is AMBIGUOUS (the symbol is present
  both before AND after the change), so _check_target_state demotes the
  target_state BLOCK to a visible advisory (passed=True + matches[].demoted)
  rather than skipping the probe. The classifier match-rule mirrors
  is_removal_intent:
    (a) modify verb anywhere in the pre-colon segment → True
    (b) modify verb as the FIRST word after the first colon → True
    (c) -ly adverb-delayed verb in the SECOND post-colon slot → True
    (d) modify verb later in a colon clause / mid colon-less clause → False

Create-intent (add / create / implement / introduce / build) is deliberately
NOT in the verb set — those goals introduce NEW symbols, so identifier presence
IS genuine duplication evidence and must still HARD-block.

Cross-refs: g-248-101 (REMOVAL sibling), rb-398 (READ carve-out pattern).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

# Neutralize gate telemetry for test invocations — without this, every suite
# run appends synthetic modify-intent-verbs firings to the PRODUCTION
# meta/gate-firings.jsonl, distorting the retirement evaluator's stats. Same
# leak-prevention the sibling test_target_state_removal_intent.py applies.
ts_mod._gate_log = lambda *args, **kwargs: None

is_modify_intent = ts_mod.is_modify_intent
is_removal_intent = ts_mod.is_removal_intent
is_read_intent = ts_mod.is_read_intent


# ─── Positive: leading post-colon modify verb → True ────────────────────

def test_apply_fix():
    assert is_modify_intent("Apply: fix the retry backoff math") is True


def test_maintain_refactor():
    assert is_modify_intent("Maintain: refactor iteration-close.sh") is True


def test_unblock_wire():
    assert is_modify_intent("Unblock: wire the reconcile endpoint") is True


def test_idea_extend():
    assert is_modify_intent("Idea: extend the parser to cover CRLF") is True


def test_apply_harden():
    assert is_modify_intent("Apply: harden the credential loader") is True


def test_apply_consolidate():
    assert is_modify_intent("Apply: consolidate the two queue writers") is True


def test_full_verb_set_leading_post_colon():
    """Every verb in the frozenset matches in the leading post-colon slot."""
    for verb in ts_mod.MODIFY_INTENT_VERBS:
        assert is_modify_intent(f"Apply: {verb} the target module") is True, verb


def test_leading_verb_with_trailing_comma():
    """Punctuation on the leading verb is stripped before matching."""
    assert is_modify_intent("Apply: fix, then re-run the suite") is True


# ─── Positive: pre-colon segment modify verb → True ─────────────────────

def test_bare_fix_no_colon():
    assert is_modify_intent("Fix the retry backoff") is True


def test_extend_no_colon():
    assert is_modify_intent("Extend the parser now") is True


def test_refactor_prefix_with_colon():
    assert is_modify_intent("Refactor parser: cleanup pass") is True


# ─── Positive: -ly adverb-delayed (2nd post-colon word) → True ──────────

def test_adverb_safely_refactor():
    assert is_modify_intent("Apply: safely refactor the loader") is True


def test_adverb_fully_rewire():
    assert is_modify_intent("Maintain: fully rewire the bridge") is True


# ─── Negative: CREATE-intent must NOT match (still hard-blocks) ─────────
# The load-bearing distinction — create verbs introduce NEW symbols, so
# identifier presence IS duplication evidence. These must stay blockable.

def test_add_not_modify():
    assert is_modify_intent("Add: new feature flag") is False


def test_apply_create_not_modify():
    assert is_modify_intent("Apply: create a new dedup gate") is False


def test_apply_implement_not_modify():
    assert is_modify_intent("Apply: implement the ranking function") is False


def test_apply_introduce_not_modify():
    assert is_modify_intent("Apply: introduce a caching layer") is False


def test_design_not_modify():
    assert is_modify_intent("Design: new schema for the ledger") is False


def test_build_not_modify():
    assert is_modify_intent("Build: the release pipeline") is False


# ─── Negative: read/removal intent titles are not modify ────────────────

def test_investigate_not_modify():
    assert is_modify_intent("Investigate: why accuracy dropped") is False


def test_retire_not_modify():
    assert is_modify_intent("Apply: retire the stale gate") is False


# ─── Negative: verb out of position / word-level / mixed intent ─────────

def test_modify_verb_later_in_clause():
    """Mixed intent keeps the check — leading post-colon word is 'add'."""
    assert is_modify_intent("Apply: add then fix the flag") is False


def test_fix_as_noun_mid_clause():
    """'fix' mid-clause with a read leading verb does not match."""
    assert is_modify_intent("Investigate: why the fix regressed") is False


def test_fixture_not_substring_matched():
    """Word-level matching — 'fixture' must not match 'fix'."""
    assert is_modify_intent("Fixture setup for the harness") is False


def test_hyphenated_compound_not_matched():
    """'wire-protocol' is one token, not the verb 'wire'."""
    assert is_modify_intent("Add: wire-protocol docs") is False


def test_no_colon_mid_verb_not_modify():
    """Colon-less title: 'fix' is mid-clause, leading word is 'scan'."""
    assert is_modify_intent("Scan and fix orphaned rows") is False


def test_no_colon_leading_modify_still_matches():
    """The colon-less leading-verb positive must hold."""
    assert is_modify_intent("Harden the session binding") is True


def test_empty_and_none():
    assert is_modify_intent("") is False
    assert is_modify_intent(None) is False


# ─── Independence: the classifiers do not bleed into each other ─────────

def test_modify_title_is_not_read_or_removal():
    assert is_read_intent("Apply: fix the retry logic") is False
    assert is_removal_intent("Apply: fix the retry logic") is False


def test_read_and_removal_regression_unchanged():
    """Sibling classifiers untouched by the new modify classifier."""
    assert is_read_intent("Investigate: foo bar") is True
    assert is_removal_intent("Apply: retire stale-read-gate") is True
    assert is_modify_intent("Investigate: foo bar") is False
    assert is_modify_intent("Apply: retire stale-read-gate") is False
