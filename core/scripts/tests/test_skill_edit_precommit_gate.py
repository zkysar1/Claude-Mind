"""Tests for skill-edit-precommit-gate.py (, earn-the-keep Phase 1 / G3).

Hermetic: loads the hyphenated module via importlib (rb-1830 — register in
sys.modules BEFORE exec_module so the eval_harness sibling import resolves), and
exercises the scoring + before/after gate decision through the head_text/new_text
test seams (no git, no daemon).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_CORE))
_spec = importlib.util.spec_from_file_location(
    "skill_edit_precommit_gate", _CORE / "skill-edit-precommit-gate.py")
seg = importlib.util.module_from_spec(_spec)
sys.modules["skill_edit_precommit_gate"] = seg  # BEFORE exec (rb-1830)
_spec.loader.exec_module(seg)


_COMPLETE = """---
name: demo
minimum_mode: autonomous
---

# /demo — Demo Skill

## Phase 1
Bash: do-the-thing.sh
Skill(next)

## Phase 2
Step 1: more work.

## Return Protocol
See .claude/rules/return-protocol.md — last action must be a tool call.
"""


def test_score_complete_skill_md_scores_high():
    s = seg.score_skill_md(_COMPLETE)
    assert s["front_matter"] == 1.0
    assert s["return_protocol"] == 1.0
    assert s["procedure"] == 1.0
    assert s["headings"] == 1.0
    assert s["body_substance"] > 0.0


def test_missing_front_matter_scores_zero():
    no_fm = _COMPLETE.split("---\n", 2)[-1]  # strip the front-matter block
    s = seg.score_skill_md(no_fm)
    assert s["front_matter"] == 0.0


def test_missing_return_protocol_scores_zero():
    no_rp = _COMPLETE.replace("## Return Protocol", "## Wrap Up")
    assert seg.score_skill_md(no_rp)["return_protocol"] == 0.0


# --------------------------------------------------------------------------- #
# before/after gate decision (no_regression)
# --------------------------------------------------------------------------- #

def test_no_change_passes():
    v = seg.evaluate_path("x/SKILL.md", head_text=_COMPLETE, new_text=_COMPLETE)
    assert v is not None and v.passed is True


def test_additive_edit_passes():
    bigger = _COMPLETE + "\n## Phase 3\nStep 2: even more.\n"
    v = seg.evaluate_path("x/SKILL.md", head_text=_COMPLETE, new_text=bigger)
    assert v.passed is True


def test_front_matter_loss_is_blocked():
    # The 2026-05-11 incident shape: an edit silently drops the YAML front matter.
    no_fm = _COMPLETE.split("---\n", 2)[-1]
    v = seg.evaluate_path("x/SKILL.md", head_text=_COMPLETE, new_text=no_fm)
    assert v.passed is False


def test_return_protocol_loss_is_blocked():
    no_rp = _COMPLETE.replace("## Return Protocol", "## Wrap Up")
    v = seg.evaluate_path("x/SKILL.md", head_text=_COMPLETE, new_text=no_rp)
    assert v.passed is False


def test_body_truncation_is_blocked():
    gutted = _COMPLETE.split("# /demo", 1)[0] + "# /demo\n\nstub.\n"
    v = seg.evaluate_path("x/SKILL.md", head_text=_COMPLETE, new_text=gutted)
    assert v.passed is False


def test_new_skill_is_skipped():
    # head_text=None AND no such file -> _git_show_head returns None for a path
    # that does not exist at HEAD; emulate via head_text sentinel by passing a
    # path git cannot resolve. Here we assert the SKIP contract directly: a None
    # head yields a None verdict.
    v = seg.evaluate_path("does/not/exist/SKILL.md", head_text=None, new_text=_COMPLETE)
    # _git_show_head on a nonexistent HEAD path returns None -> SKIP
    assert v is None


def test_main_returns_zero_when_no_skill_staged(monkeypatch):
    monkeypatch.setattr(seg, "_staged_skill_mds", lambda: [])
    assert seg.main([]) == 0


def test_main_blocks_on_regression(monkeypatch, tmp_path):
    # Stage one path; HEAD complete, working-tree gutted -> regression -> exit 1.
    p = "x/SKILL.md"
    monkeypatch.setattr(seg, "_staged_skill_mds", lambda: [p])
    monkeypatch.setattr(seg, "_git_show_head", lambda path: _COMPLETE)
    monkeypatch.setattr(seg.Path, "read_text", lambda self, **kw: "stub")
    monkeypatch.delenv("SKILL_EDIT_GATE_OVERRIDE", raising=False)
    assert seg.main([p]) == 1


def test_override_allows_regression(monkeypatch):
    p = "x/SKILL.md"
    monkeypatch.setattr(seg, "_git_show_head", lambda path: _COMPLETE)
    monkeypatch.setattr(seg.Path, "read_text", lambda self, **kw: "stub")
    monkeypatch.setenv("SKILL_EDIT_GATE_OVERRIDE", "deliberate consolidation")
    assert seg.main([p]) == 0
