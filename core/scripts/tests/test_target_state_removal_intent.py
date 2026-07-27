"""test_target_state_removal_intent.py — REMOVAL_INTENT_VERBS carve-out.

Background (g-248-101, 2026-07-13):
  Removal goals invert the target_state semantic the same way READ goals do
  (rb-404 / rb-398), from the other side: the named identifiers are present
  in the target files BECAUSE they are the removal target — presence means
  NOT done. Session 104 hit this 3× in one day, each needing a manual
  --override-duplication (g-248-100, g-115-2107, g-115-2108). The canonical
  shape is g-115-2107 "Apply: retire stale-read-gate" — a generic label
  prefix with the removal verb as the FIRST post-colon word.

Position contract pinned here:
  (a) removal verb anywhere in the pre-colon segment → True
  (b) removal verb as the FIRST word after the first colon → True
  (c) removal verb later in the post-colon clause → False (mixed intent
      keeps the check — same conservatism as is_read_intent's
      "Fix: review the retry logic" example)

Cross-refs: rb-404 (READ-intent carve-out pattern), g-248-101 (origin).
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
# run appends ~20 synthetic removal-intent-verbs firings to the PRODUCTION
# meta/gate-firings.jsonl, distorting the retirement evaluator's stats for
# this gate (observed: the very first pytest run wrote 17 records). The
# sibling test_target_state_check_positional.py has the same leak for
# read-intent-verbs — tracked separately as a systemic fix.
ts_mod._gate_log = lambda *args, **kwargs: None

is_removal_intent = ts_mod.is_removal_intent
is_read_intent = ts_mod.is_read_intent


# ─── Positive cases: leading post-colon removal verb → True ─────────────

def test_apply_retire_canonical_fp():
    """The canonical session-104 FP shape ()."""
    assert is_removal_intent("Apply: retire stale-read-gate — full removal scope") is True


def test_maintain_remove():
    assert is_removal_intent("Maintain: remove dead flag from iteration-close.sh") is True


def test_unblock_delete():
    assert is_removal_intent("Unblock: delete orphaned rows from store") is True


def test_idea_deprecate():
    assert is_removal_intent("Idea: deprecate old endpoint in favor of v2") is True


def test_idea_strip():
    assert is_removal_intent("Idea: strip CRLF handling from wrapper") is True


def test_maintain_purge():
    assert is_removal_intent("Maintain: purge stale cache entries") is True


def test_leading_verb_with_trailing_comma():
    """Punctuation on the leading verb is stripped before matching."""
    assert is_removal_intent("Apply: retire, then clean up references") is True


# ─── Positive cases: pre-colon segment removal verb → True ──────────────

def test_bare_retire_no_colon():
    assert is_removal_intent("Retire stale-read-gate") is True


def test_remove_prefix_with_colon():
    assert is_removal_intent("Remove legacy fallback: scope list") is True


def test_drop_prefix():
    assert is_removal_intent("Drop legacy fallback: cleanup pass") is True


# ─── Negative cases: verb absent or out of position → False ─────────────

def test_removal_verb_later_in_clause():
    """Mixed intent keeps the check — leading post-colon word is 'review'."""
    assert is_removal_intent("Fix: review then remove the flag") is False


def test_add_goal_mentioning_retirement():
    """'retirement' as a noun mid-clause must not match; leading verb is 'add'."""
    assert is_removal_intent("Apply: add retirement docs for the gate") is False


def test_drop_as_noun_mid_clause():
    """'drop' IS in the verb set but appears mid-clause — position rule holds."""
    assert is_removal_intent("Investigate: why the drop in accuracy") is False


def test_dropdown_not_substring_matched():
    """Word-level matching — 'dropdown' must not match 'drop'."""
    assert is_removal_intent("Improve dropdown rendering") is False


def test_hyphenated_compound_not_matched():
    """'remove-button' is one token, not the verb 'remove'."""
    assert is_removal_intent("Idea: add remove-button to UI") is False


def test_plain_implementation_goal():
    assert is_removal_intent("Fix: correct the retry backoff math") is False


def test_empty_and_none():
    assert is_removal_intent("") is False
    assert is_removal_intent(None) is False


# ─── : adverb-prefixed removal (2nd post-colon word) ───────────
# An adverbial modifier can delay the removal verb into the SECOND post-colon
# slot. Admit word[1] as a candidate ONLY when word[0] ends in "-ly" (a
# surgical context-gate, not a blanket first-two-words widen — see the
# adversarial controls below). Origin: sq-016 FP-class re-opening.

def test_adverb_fully_retire():
    assert is_removal_intent("Apply: fully retire X") is True


def test_adverb_safely_delete():
    assert is_removal_intent("Maintain: safely delete Y") is True


def test_adverb_cleanly_remove():
    assert is_removal_intent("Idea: cleanly remove the shim") is True


# ─── : adversarial precision controls (guard-958) ──────────────
# The -ly gate is deliberately narrower than a blanket "first-two-post-colon-
# words" widen: a noun-phrase implementation title whose SECOND word is a
# removal verb (a compound noun like "soft delete" / "hard delete") is a
# genuine target_state goal, NOT a removal. word[0] here is an ADJECTIVE
# ("soft"/"hard"), not a -ly adverb, so no 2nd-word candidate is admitted and
# the check correctly stays. This is the precision control the block comment
# in _target_state.is_removal_intent names.

def test_add_soft_delete_not_removal():
    """'soft delete' is a feature noun-phrase — word[0]='soft' is not -ly."""
    assert is_removal_intent("Add: soft delete support") is False


def test_fix_hard_delete_not_removal():
    """'hard delete' likewise — 'hard' is an adjective, not a -ly adverb."""
    assert is_removal_intent("Fix: hard delete perf regression") is False


# ───  finding 4: no-colon over-match ──────────────────────────
# A removal verb in a subordinate clause of a COLON-LESS title is not the
# primary action. The primary action of a colon-less title is its LEADING
# word only (mirrors the same fix applied to is_read_intent). Before this
# fix "Scan and remove orphaned rows" matched 'remove' and returned True.

def test_no_colon_mid_verb_not_removal():
    """Colon-less title: 'remove' is mid-clause, leading word is 'scan'."""
    assert is_removal_intent("Scan and remove orphaned rows") is False


def test_no_colon_leading_removal_still_matches():
    """The finding-4 fix must NOT regress the bare-leading-verb positive."""
    assert is_removal_intent("Remove orphaned session bindings") is True


# ─── Independence: the two classifiers do not bleed into each other ─────

def test_read_intent_title_is_not_removal():
    assert is_removal_intent("Investigate: foo bar") is False


def test_removal_title_is_not_read_intent():
    assert is_read_intent("Apply: retire stale-read-gate") is False


def test_read_intent_regression_unchanged():
    """READ-intent contract untouched by the sibling classifier."""
    assert is_read_intent("Investigate: foo bar") is True
    assert is_read_intent("Audit: schema drift") is True
