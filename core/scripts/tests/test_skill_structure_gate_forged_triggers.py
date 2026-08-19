"""Tests for the `forged_triggers_present` check in core/scripts/skill-structure-gate.py.

Added by g-115-4436. That goal asked for TWO floors at registration and only one
of them survives, so this file pins the split as much as the behaviour:

  * EMPTINESS FLOOR (built, pinned here) — a forged-skills.yaml row whose
    `triggers` is absent, empty, non-list, or blank-only is UNREACHABLE by
    `.claude/rules/forged-skill-resolution.md`, which routes natural-language
    actions to forged skills by matching `triggers`. g-115-3858 cleaned that
    state by hand and nothing stopped it recurring — forge-skill/SKILL.md asks
    for triggers in prose, which is not a gate.

  * WORD-COUNT FLOOR (deliberately NOT built, pinned here as an ANTI-test) —
    g-115-4436 also asked for ">=2 words after normalization", reusing
    forged-skill-surface.py's `_norm` + `MIN_TRIGGER_WORDS`. g-115-4446 measured
    five candidate matchers on a 30-goal hand-labelled sample and none cleared
    the bar (shipped matcher recall 0.00, best precision 0.12), so g-115-4475
    retired matching entirely (commit bf314aceb) and
    test_forged_skill_surface.py::test_no_matching_symbols_remain fails if the
    symbols return. The retirement also INVERTS the premise: the consumer is now
    an LLM, not a lexical matcher, so a distinctive single token ("journalctl"
    for ssm-run) is a HIGH-precision trigger rather than dead weight.

The anti-test matters more than it looks. A word-count threshold rebuilt HERE
would satisfy every other test in this file and would sit outside the reach of
the sibling's mutation guard, which only inspects forged-skill-surface. That is
exactly the "one gate over" drift this file exists to catch.

Hermetic tests use synthetic registries; the single live-registry test at the
bottom exists because without it every hermetic test here would still pass while
production carried an unreachable skill.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "skill_structure_gate", _SCRIPTS / "skill-structure-gate.py")
ssg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssg)


def _skills(**rows):
    return dict(rows)


def _names(violations):
    return {v["skill"] for v in violations}


# ── The emptiness floor ────────────────────────────────────────────────────

def test_empty_triggers_list_is_refused():
    v = ssg._check_forged_triggers(_skills(alpha={"triggers": []}))
    assert _names(v) == {"alpha"}
    assert "empty list" in v[0]["detail"]


def test_missing_triggers_key_is_refused():
    v = ssg._check_forged_triggers(_skills(alpha={"description": "no triggers"}))
    assert _names(v) == {"alpha"}
    assert "no `triggers` key" in v[0]["detail"]


def test_blank_only_triggers_are_refused():
    v = ssg._check_forged_triggers(_skills(alpha={"triggers": ["", "   ", "\t"]}))
    assert _names(v) == {"alpha"}
    assert "no non-blank string" in v[0]["detail"]


def test_non_list_triggers_are_refused():
    """A bare string is iterable, so a naive `any(...)` over it would pass on
    its characters. The check must reject the SHAPE, not sniff the contents."""
    v = ssg._check_forged_triggers(_skills(alpha={"triggers": "notify the user"}))
    assert _names(v) == {"alpha"}
    assert "not a list" in v[0]["detail"]


def test_usable_triggers_pass():
    v = ssg._check_forged_triggers(
        _skills(alpha={"triggers": ["notify the user", "send an alert"]}))
    assert v == []


def test_one_usable_trigger_among_blanks_passes():
    """The floor is reachability, not hygiene: one live handle is enough."""
    v = ssg._check_forged_triggers(_skills(alpha={"triggers": ["", "notify the user"]}))
    assert v == []


def test_non_dict_row_is_skipped_not_crashed():
    v = ssg._check_forged_triggers(_skills(alpha=None, bravo=["junk"]))
    assert v == []


# ── The anti-test: no word-count floor, ever ───────────────────────────────

def test_single_word_trigger_is_NOT_refused():
    """THE LOAD-BEARING ANTI-TEST. Rebuilding the retired matcher's >=2-word
    threshold here would make this fail and every other test in this file pass.
    Live examples: ssm-run carries 'journalctl' and 'systemctl'; scan-stale-jobs
    carries 'stale' and 'reap'. Under an LLM consumer these are high-precision,
    not dead weight (g-115-4475 retired the lexical matcher that thought
    otherwise; it measured recall 0.00)."""
    v = ssg._check_forged_triggers(
        _skills(ssm_run={"triggers": ["journalctl"]},
                scan_stale_jobs={"triggers": ["stale", "reap"]}))
    assert v == [], f"a word-count floor was reintroduced: {v}"


def test_retired_matcher_symbols_are_not_reintroduced_here():
    """Sibling of test_forged_skill_surface.py::test_no_matching_symbols_remain,
    aimed at THIS module — the place the retired matcher would most plausibly be
    rebuilt, and the place that guard cannot see."""
    for gone in ("_norm", "MIN_TRIGGER_WORDS", "match_skills", "_goal_text"):
        assert not hasattr(ssg, gone), (
            f"{gone} appeared in skill-structure-gate — the forged-skill matcher "
            "was retired by g-115-4475; do not rebuild it one gate over")


# ── Wiring ─────────────────────────────────────────────────────────────────

def test_check_is_registered_in_all_checks():
    """An unregistered check is silently never run by --check's default."""
    assert "forged_triggers_present" in ssg.ALL_CHECKS


def test_only_name_narrows_to_a_single_row():
    rows = _skills(alpha={"triggers": []}, bravo={"triggers": []})
    assert _names(ssg._check_forged_triggers(rows, only_name="alpha")) == {"alpha"}


def test_only_name_for_absent_row_yields_nothing():
    """--skill may name a directory that has no forged registry row at all."""
    assert ssg._check_forged_triggers(_skills(alpha={"triggers": []}),
                                      only_name="nonexistent") == []


def test_loader_is_fail_open_on_missing_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(ssg, "FORGED_REGISTRY", tmp_path / "does-not-exist.yaml")
    assert ssg._load_forged_skill_rows() == {}
    assert ssg._load_forged_skill_names() == set()


def test_loader_is_fail_open_on_unparseable_registry(monkeypatch, tmp_path):
    reg = tmp_path / "forged-skills.yaml"
    reg.write_text("skills: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(ssg, "FORGED_REGISTRY", reg)
    assert ssg._load_forged_skill_rows() == {}


def test_names_loader_still_returns_names(monkeypatch, tmp_path):
    """_load_forged_skill_names was refactored onto _load_forged_skill_rows;
    its existing callers must be unaffected."""
    reg = tmp_path / "forged-skills.yaml"
    reg.write_text("skills:\n  alpha:\n    triggers: ['do a thing']\n", encoding="utf-8")
    monkeypatch.setattr(ssg, "FORGED_REGISTRY", reg)
    assert ssg._load_forged_skill_names() == {"alpha"}


# ── Live registry ──────────────────────────────────────────────────────────

def test_live_registry_has_no_unreachable_forged_skills():
    """Without this, every hermetic test above passes while production carries a
    skill no rule can route to. Measured 2026-08-10: 61 skills, 0 violations."""
    rows = ssg._load_forged_skill_rows()
    if not rows:
        pytest.skip("no live forged-skills registry resolvable in this environment")
    v = ssg._check_forged_triggers(rows)
    assert v == [], f"unreachable forged skills in the live registry: {_names(v)}"
