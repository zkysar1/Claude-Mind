"""test_target_state_build_test_intent.py — BUILD_INTENT / TEST_AUTHORING carve-out.

Background (g-115-2869, 2026-07-22):
  A goal to BUILD a new artifact (gate / check / module / script / detector ...)
  or to ADD an integration test NAMES the EXISTING file it will touch — and that
  file's symbols are the integration SURFACE, present in the target file BEFORE
  the work, not the deliverable. Presence proves neither duplication NOR
  completion (the NEW artifact/test is absent; the named existing symbols are
  what it integrates with or exercises) — the SAME ambiguity is_modify_intent
  handles, so the caller DEMOTES the target_state block to a visible advisory
  rather than skipping. Two 2026-07 filings hard-blocked (400) on this exact FP
  and cleared only after re-wording:
    g-115-2857  "Idea: goal-creation gate refusing high-blast-radius ..."   (build)
    g-115-2862  "Idea: integration test proving the fabricated-approval ..." (test)

CRITICAL DIFFERENCE from the read/removal/modify siblings — those are VERB-led
  (the primary ACTION verb names the intent). Build/test titles are frequently
  NOUN-led: the deliverable NOUN names the intent with no explicit build verb
  ("goal-creation gate refusing X", "integration test proving Y"). So this
  detector matches a build VERB *or* a deliverable NOUN (gate/check/module/...)
  *or* the noun "test" in the leading segment:
    (a) any word in the pre-colon segment → match
    (b) any of the FIRST THREE post-colon words → match  (wider than the verb
        siblings' word-1 window, because a deliverable noun carries a compound
        adjectival modifier: "goal-creation gate", "integration test")
    (c) colon-less title → the leading word only (subordinate-clause
        conservatism, same as the siblings)

Supersession of the modify test's "create must HARD-block" note: that blanket was
  the OLD philosophy. g-115-2869 refines it for the target_state check — a
  create-a-new-ARTIFACT goal that touches an existing file DEMOTES (not skips),
  so the FP risk is bounded (match stays visible + the other 4 dup checks apply;
  git_log_48h catches a genuinely-just-built duplicate). is_modify_intent itself
  is UNTOUCHED (create verbs are still not modify — see the sibling test), and in
  the gate the modify branch returns BEFORE this one, so a mixed "Fix: ... gate"
  title classifies as modify (precedence), never build.

Cross-refs: g-115-2565 (MODIFY sibling — same DEMOTE contract), g-248-101
  (REMOVAL sibling), rb-398 (READ carve-out pattern).
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

# Neutralize gate telemetry for test invocations — without this, every suite run
# appends synthetic build-test-authoring-intent firings to the PRODUCTION
# meta/gate-firings.jsonl, distorting the retirement evaluator's stats. Same
# leak-prevention the sibling test_target_state_{modify,removal}_intent.py apply.
ts_mod._gate_log = lambda *args, **kwargs: None

f = ts_mod.is_build_or_test_authoring_intent
is_modify_intent = ts_mod.is_modify_intent
is_removal_intent = ts_mod.is_removal_intent
is_read_intent = ts_mod.is_read_intent


# ─── Anchor: the two REAL 2026-07 filings that hard-blocked () ─────

def test_real_g_115_2857_build_gate():
    assert f("Idea: goal-creation gate refusing high-blast-radius "
             "approval-asserting goals that lack a verifiable approval "
             "reference") == "build-intent"


def test_real_g_115_2862_integration_test():
    assert f("Idea: integration test proving the fabricated-approval advisory "
             "fires end-to-end through the goal-filing path") == "test-authoring"


# ─── Positive: build deliverable NOUN in leading post-colon segment ─────────

def test_noun_gate_word_two():
    assert f("Idea: goal-creation gate refusing X") == "build-intent"


def test_noun_module_word_four_still_matches_within_window():
    # "create a new module" — module is post-colon word 4? create/a/new/module.
    # First-THREE window = [create, a, new]; "create" (verb) matches first.
    assert f("Apply: create a new module") == "build-intent"


def test_noun_check_with_compound_modifier():
    assert f("Idea: the admission check rejecting stale tokens") == "build-intent"


def test_full_build_noun_set_leading():
    for noun in ts_mod.BUILD_INTENT_NOUNS:
        assert f(f"Idea: new {noun} for the loader") == "build-intent", noun


def test_pre_colon_build_noun():
    assert f("New dedup gate: scope note") == "build-intent"


# ─── Positive: build VERB (verb-led form) ───────────────────────────────────

def test_full_build_verb_set_leading_post_colon():
    for verb in ts_mod.BUILD_INTENT_VERBS:
        assert f(f"Apply: {verb} the ranking helper") == "build-intent", verb


def test_verb_create_no_colon():
    assert f("Create the release pipeline now") == "build-intent"


# ─── Positive: TEST-authoring noun ──────────────────────────────────────────

def test_integration_test():
    assert f("Idea: integration test proving the advisory fires") == "test-authoring"


def test_unit_tests_plural():
    assert f("Apply: unit tests for the parser") == "test-authoring"


def test_regression_test_word_two():
    assert f("Maintain: regression test for the sweep") == "test-authoring"


def test_test_precedence_over_build_noun():
    """A title carrying BOTH a test noun and a build noun → test-authoring."""
    assert f("Idea: integration test for the dedup gate") == "test-authoring"


# ─── Negative: no build/test signal in the leading segment → None ───────────

def test_add_rate_limiting_not_build():
    # add/rate/limiting — no artifact noun, no build verb, no test noun.
    assert f("Idea: add rate limiting to login.py") is None


def test_add_soft_delete_support_not_build():
    assert f("Add: soft delete support to store.py") is None


def test_generic_fix_prose_not_build():
    assert f("Apply: reduce the retry backoff") is None


def test_noun_deep_in_prose_clause_not_matched():
    """A build noun past the first-three post-colon window does not over-exempt."""
    assert f("Idea: reduce latency on the request path that the gate guards") is None


def test_colon_less_noun_mid_clause_not_matched():
    """Colon-less: only the leading word counts; a mid-clause 'gate' is not it."""
    assert f("Reduce latency on the auth gate") is None


def test_empty_and_none():
    assert f("") is None
    assert f(None) is None


def test_substring_not_matched():
    """Word-level matching — 'gateway'/'testable' must not match 'gate'/'test'."""
    assert f("Idea: gateway config for the proxy") is None
    assert f("Idea: make the loader testable") is None


# ─── Independence: classifiers do not bleed into each other ─────────────────

def test_build_title_is_not_read_removal_or_modify():
    t = "Idea: goal-creation gate refusing X"
    assert is_read_intent(t) is False
    assert is_removal_intent(t) is False
    assert is_modify_intent(t) is False


def test_test_title_is_not_read_removal_or_modify():
    t = "Idea: integration test proving Y"
    assert is_read_intent(t) is False
    assert is_removal_intent(t) is False
    assert is_modify_intent(t) is False


def test_read_removal_modify_regression_unchanged():
    """Sibling classifiers untouched by the new build/test classifier."""
    assert is_read_intent("Investigate: foo bar") is True
    assert is_removal_intent("Apply: retire stale-read-gate") is True
    assert is_modify_intent("Apply: fix the retry logic") is True
    # And those sibling-matched titles are NOT (mis)classified as build/test by
    # the new detector when their own verb leads (the gate checks read/removal
    # SKIP and modify DEMOTE before reaching this one, so precedence is by
    # ordering, but the classifier itself must also not claim a pure read title).
    assert f("Investigate: why accuracy dropped") is None
    # 'gate' is post-colon word 4 (retire/the/stale/gate), OUTSIDE the leading-3
    # window → None. When a build noun IS in-window on a removal title
    # ("Apply: retire gate X"), the gate's removal-SKIP branch runs BEFORE this
    # DEMOTE branch, so removal precedence is by ordering, not by the classifier.
    assert f("Apply: retire the stale gate") is None
