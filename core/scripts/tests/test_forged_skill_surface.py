"""Tests for core/scripts/forged-skill-surface.py — the Phase 4 forged-skill reader.

Pins the two behavioural cases g-115-3811's verification names explicitly
("a trigger-matching goal surfaces the skill; a non-matching goal surfaces
nothing"), plus the two decisions that are load-bearing and would otherwise
silently regress:

  * the >=2-word trigger floor (MIN_TRIGGER_WORDS). Without it the matcher
    fires on 26.4% of the goal corpus instead of 11.4%, because a one-word
    trigger like 'stale' matches almost any goal text.
  * the space-padding in _norm, which is what makes containment a
    word-boundary test rather than a substring test.

These use a synthetic registry, NOT world/forged-skills.yaml — the live
registry is domain state that changes underneath the framework, and a test
pinned to it would fail for reasons unrelated to this code.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "forged-skill-surface.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("forged_skill_surface", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fss = _load_module()


@pytest.fixture
def registry(monkeypatch):
    """Install a synthetic forged-skills registry."""
    entries = [
        {
            "skill": "access-aws-services",
            "triggers": ["aws cli", "call aws", "list s3 bucket"],
            "scripts": ["world/scripts/aws-exec.sh"],
        },
        {
            "skill": "scan-stale-jobs",
            # 'stale' is single-word — the degenerate shape MIN_TRIGGER_WORDS exists for.
            "triggers": ["stale", "reap", "stale-jobs"],
            "scripts": ["world/scripts/scan-stale.sh"],
        },
        {
            "skill": "no-triggers-at-all",
            # Mirrors run-game-session in the live registry ().
            "triggers": [],
            "scripts": ["world/scripts/whatever.sh"],
        },
    ]
    monkeypatch.setattr(fss, "_load_forged_skills", lambda _wdir: entries)
    return entries


def _skills(matches):
    return {m["skill"] for m in matches}


# ── The two cases the goal's verification names ────────────────────────────

def test_trigger_matching_goal_surfaces_the_skill(registry):
    m = fss.match_skills("I need to call aws cli and list s3 bucket contents", None)
    assert "access-aws-services" in _skills(m)
    hit = next(x for x in m if x["skill"] == "access-aws-services")
    assert "aws cli" in hit["matched_triggers"]
    assert hit["scripts"] == ["world/scripts/aws-exec.sh"]


def test_non_matching_goal_surfaces_nothing(registry):
    m = fss.match_skills("reconcile knowledge tree node summaries after a rename", None)
    assert m == []


# ── The >=2-word floor ─────────────────────────────────────────────────────

def test_single_word_trigger_does_not_match(registry):
    """'stale' must NOT drag in scan-stale-jobs on unrelated text.

    Mutation check: raising this file's expectation requires MIN_TRIGGER_WORDS
    to actually be enforced. Set MIN_TRIGGER_WORDS = 1 and this test fails.
    """
    m = fss.match_skills("the handoff notes went stale over the weekend", None)
    assert "scan-stale-jobs" not in _skills(m)


def test_multi_word_trigger_on_same_skill_still_matches(registry):
    """The floor drops only the degenerate triggers, not the whole skill."""
    m = fss.match_skills("sweep the stale jobs queue this iteration", None)
    assert "scan-stale-jobs" in _skills(m)


def test_min_trigger_words_constant_is_two():
    assert fss.MIN_TRIGGER_WORDS == 2


# ── Word-boundary property of _norm ────────────────────────────────────────

def test_containment_is_word_boundary_not_substring(registry):
    """'flaws climate' contains the substring 'aws cli' — it must not match."""
    m = fss.match_skills("the flaws climate models exhibit are documented", None)
    assert m == []


def test_norm_pads_and_collapses():
    assert fss._norm("Aws-CLI!") == " aws cli "
    assert fss._norm("") == "  "


# ── Robustness: the advisory must never raise ──────────────────────────────

def test_empty_trigger_list_never_matches(registry):
    m = fss.match_skills("run no-triggers-at-all whatever it takes", None)
    assert "no-triggers-at-all" not in _skills(m)


def test_empty_text_returns_empty(registry):
    assert fss.match_skills("", None) == []


def test_registry_load_failure_is_swallowed(monkeypatch):
    def boom(_wdir):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(fss, "_load_forged_skills", boom)
    assert fss.match_skills("call aws cli", None) == []


def test_absent_loader_returns_empty(monkeypatch):
    monkeypatch.setattr(fss, "_load_forged_skills", None)
    assert fss.match_skills("call aws cli", None) == []


# ── Ordering ───────────────────────────────────────────────────────────────

def test_more_matched_triggers_sorts_first(registry):
    m = fss.match_skills("aws cli and call aws and stale jobs sweep", None)
    assert m[0]["skill"] == "access-aws-services"
    assert len(m[0]["matched_triggers"]) >= 2


# ── Rule-name triggers fire on CITATIONS, so they are excluded () ──
#
# Measured on the live corpus: 'archive before delete' is the basename of
# .claude/rules/archive-before-delete.md, produced 52 of ~100 total trigger
# hits across 663 live goals, and 11 of 12 sampled contexts were CITATIONS of
# the rule ("...applies if any row is removed", "inverts archive-before-delete
# md") rather than requests to invoke the graveyard skill. Excluding it moved
# the fire rate 13.1% -> 10.0% with no loss of genuine reach.
#
# The pair below is the guard-1451 refuse/allow pair on ONE skill: the same
# entry must lose its rule-named trigger and keep its action-shaped one, so a
# fix that simply dropped the skill would fail the allow case.

@pytest.fixture
def citation_registry(monkeypatch):
    """A skill carrying BOTH a rule-named trigger and an action-shaped one."""
    entries = [
        {
            "skill": "archive-graveyard",
            "triggers": ["archive before delete", "graveyard this", "purge the bucket"],
            "scripts": ["world/scripts/graveyard.sh"],
        },
    ]
    monkeypatch.setattr(fss, "_load_forged_skills", lambda _wdir: entries)
    # Hermetic: pin the rule-name set instead of reading the real rules dir,
    # so the test does not change meaning when a rule file is added/renamed.
    monkeypatch.setattr(fss, "_rule_phrases_cache", {"archive before delete"})
    return entries


def test_rule_named_trigger_does_not_match_a_citation(citation_registry):
    """REFUSE: the phrase is contiguously present, and must still not match."""
    text = "whichever is chosen, archive-before-delete applies to any row removal"
    assert "archive before delete" in fss._norm(text)  # the phrase IS there
    assert fss.match_skills(text, None) == []


def test_action_shaped_trigger_on_the_same_skill_still_matches(citation_registry):
    """ALLOW: excluding the rule-named trigger must not mute the skill."""
    m = fss.match_skills("graveyard this env dir and purge the bucket", None)
    assert _skills(m) == {"archive-graveyard"}
    hit = m[0]
    assert "archive before delete" not in hit["matched_triggers"]
    assert set(hit["matched_triggers"]) == {"graveyard this", "purge the bucket"}


def test_rule_name_phrases_reads_the_real_rules_dir():
    """The mechanism's live input is non-empty and carries the measured case.

    Hermetic tests pin the cache, so without this one the exclusion set could
    silently become empty in production and every test above would still pass.
    """
    fss._rule_phrases_cache = None  # force a real read
    try:
        phrases = fss.rule_name_phrases()
        assert len(phrases) > 10, f"rules dir read returned only {len(phrases)}"
        assert "archive before delete" in phrases
    finally:
        fss._rule_phrases_cache = None
