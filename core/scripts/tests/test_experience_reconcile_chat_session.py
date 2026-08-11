"""test_experience_reconcile_chat_session.py -- regression for .

experience-reconcile.py carried its OWN copy of the experience type enum, and the
two copies drifted the day after the file was written: `chat_session` entered
experience.py on 2026-05-08 (50ba8a0b1), one day after experience-reconcile.py
landed (86a6e5950, 2026-05-07), and was never mirrored into the audit's set.

Consequence, measured 2026-08-02 (bravo, hostname cc-05, uname -r
6.8.0-136-generic): the write side accepted 15 chat_session records into the live
store while the audit reported 9 orphan .md files as `type unrecoverable` -- a
class that could never resolve, because reconcile's two rescues (hypothesis_id ->
hypothesis_formation, goal_id -> goal_execution) both key on fields a chat_session
carries by construction-free. (test_experience_goal_id_backfill.py independently
pins `exp-encode-session-*` ids as "slug-only, genuinely goal-less".)

Fix, both halves pinned here:
  1. reconcile imports VALID_TYPES from experience.py (SSOT), so the two sets
     cannot diverge again -- test_valid_types_is_ssot fails on re-duplication.
  2. A chat_session with no resolvable category falls back to the "chat-session"
     category. Without this the record defers one gate later as `category
     unrecoverable`: 8 of the 9 orphans carry no category in front matter, so the
     VALID_TYPES fix ALONE would have rescued exactly 1 of 9 and left the
     deferred count unchanged.

Measured effect on the live bravo store: backfilled 442 -> 451, deferred
106 -> 97 (delta of exactly 9, the chat_session orphan population, no collateral).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import experience  # conftest puts core/scripts on sys.path

_SPEC = importlib.util.spec_from_file_location(
    "experience_reconcile",
    Path(__file__).resolve().parents[1] / "experience-reconcile.py",
)
reconcile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconcile)


# --------------------------------------------------------------------------
# Part 1: the SSOT pin -- the actual regression guard for the drift class
# --------------------------------------------------------------------------

def test_valid_types_is_ssot():
    """reconcile must not re-introduce its own copy of the type enum.

    Identity, not equality: an equal-but-separate literal would pass an `==`
    check on the day it was written and then drift silently, which is precisely
    the failure this test exists to prevent.
    """
    assert reconcile.VALID_TYPES is experience.VALID_TYPES


def test_chat_session_is_a_valid_reconcile_type():
    assert "chat_session" in reconcile.VALID_TYPES


# --------------------------------------------------------------------------
# Part 2: the ladder, exercised through reconcile_agent's real code path
# --------------------------------------------------------------------------

def _mk_agent(tmp_path, monkeypatch, md_body, jsonl_lines=""):
    """Build a throwaway agent dir and point the module's resolvers at it.

    PROJECT_ROOT is monkeypatched too because reconcile_agent computes
    `md_path.relative_to(PROJECT_ROOT)`, which raises ValueError for a tmp dir
    outside the real root.
    """
    agent_dir = tmp_path / "agents" / "testagent"
    (agent_dir / "experience").mkdir(parents=True)
    (agent_dir / "experience.jsonl").write_text(jsonl_lines, encoding="utf-8")
    (agent_dir / "experience" / "exp-encode-session-fixture.md").write_text(
        md_body, encoding="utf-8")
    monkeypatch.setattr(reconcile, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(reconcile, "_agent_dir", lambda name: agent_dir)
    monkeypatch.setattr(reconcile, "load_goal_index", lambda: {})
    return agent_dir


CHAT_SESSION_MD = """---
type: chat_session
agent: testagent
date: 2026-08-02
---

# A chat session with no category and no goal_id

Body text.
"""


def test_chat_session_orphan_reconciles(tmp_path, monkeypatch):
    """The measured shape: type=chat_session, no category, no goal_id, no hypothesis_id."""
    _mk_agent(tmp_path, monkeypatch, CHAT_SESSION_MD)
    result = reconcile.reconcile_agent("testagent", apply=False)
    assert result["actions"]["jsonl_records_backfilled"] == 1
    assert result["actions"]["deferred"] == 0


def test_chat_session_fallback_does_not_override_declared_category(tmp_path, monkeypatch):
    """A chat_session that DOES declare a category keeps it (fallback is last-resort)."""
    md = CHAT_SESSION_MD.replace("date: 2026-08-02",
                                 "date: 2026-08-02\ncategory: capability-routing")
    _mk_agent(tmp_path, monkeypatch, md)
    result = reconcile.reconcile_agent("testagent", apply=False)
    assert result["actions"]["jsonl_records_backfilled"] == 1
    assert result["actions"]["deferred"] == 0


def test_unknown_type_without_category_still_defers(tmp_path, monkeypatch):
    """The fallback is narrow: it must not rescue every category-less orphan.

    Guards against widening the fix into `category = category or rec_type`, which
    would silently reclassify the 77 unrelated `category unrecoverable` orphans
    measured alongside this fix.
    """
    md = CHAT_SESSION_MD.replace("type: chat_session", "type: not_a_real_type")
    _mk_agent(tmp_path, monkeypatch, md)
    result = reconcile.reconcile_agent("testagent", apply=False)
    assert result["actions"]["jsonl_records_backfilled"] == 0
    assert result["actions"]["deferred"] == 1


def test_valid_type_without_category_still_defers(tmp_path, monkeypatch):
    """A goal-less `research` orphan has no category source and must still defer."""
    md = CHAT_SESSION_MD.replace("type: chat_session", "type: research")
    _mk_agent(tmp_path, monkeypatch, md)
    result = reconcile.reconcile_agent("testagent", apply=False)
    assert result["actions"]["jsonl_records_backfilled"] == 0
    assert result["actions"]["deferred"] == 1
