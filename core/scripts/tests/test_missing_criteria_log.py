"""Tests for missing-criteria-log.py ( / BRD Gap 15).

The appender records *uncovered* generated-checklist failures from
aspirations-verify Q1.5 to meta/missing-verification-criteria.jsonl. These
tests pin the pure validator (`build_record`) and the locked append
(`append_record`) against a tmp_path — no META resolution, no daemon, so the
suite stays hermetic.

Pattern: same importlib + sys.path shape as test_defer_drift_check.py (the
script name has hyphens, so it cannot be a plain `import`).
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "missing-criteria-log.py"

# Fixed reference time so the stamped id / logged_at are deterministic.
NOW = dt.datetime(2026, 6, 12, 9, 30, 0)


def _import():
    spec = importlib.util.spec_from_file_location("missing_criteria_log", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["missing_criteria_log"] = mod
    spec.loader.exec_module(mod)
    return mod


def _payload(**kw):
    p = {
        "goal_id": "g-306-03",
        "items": [
            "aspirations-verify SKILL.md has a Q1.5 step between Q1 and Q2",
            "the companion logger script exists",
        ],
        "source": "world",
        "category": "framework-patterns",
        "artifact": ".claude/skills/aspirations-verify/SKILL.md",
    }
    p.update(kw)
    return p


# ── build_record (the validator + stamper) ─────────────────────────────────

def test_build_record_canonical():
    mod = _import()
    rec = mod.build_record(_payload(), NOW)
    assert rec["goal_id"] == "g-306-03"
    assert rec["id"] == "mvc-g-306-03-20260612093000"
    assert rec["logged_at"] == "2026-06-12T09:30:00"
    assert rec["items"] == [
        "aspirations-verify SKILL.md has a Q1.5 step between Q1 and Q2",
        "the companion logger script exists",
    ]
    assert rec["source"] == "world"
    assert rec["category"] == "framework-patterns"
    assert rec["artifact"] == ".claude/skills/aspirations-verify/SKILL.md"


def test_build_record_strips_and_drops_blank_items():
    mod = _import()
    rec = mod.build_record(_payload(items=["  kept  ", "", "   ", "also kept"]), NOW)
    assert rec["items"] == ["kept", "also kept"]


def test_build_record_omits_absent_optional_fields():
    mod = _import()
    rec = mod.build_record({"goal_id": "g-1-1", "items": ["x"]}, NOW)
    assert "source" not in rec and "category" not in rec
    assert "artifact" not in rec and "note" not in rec


def test_build_record_rejects_missing_goal_id():
    mod = _import()
    for bad in ({}, {"goal_id": "", "items": ["x"]}, {"goal_id": "   ", "items": ["x"]}):
        with pytest.raises(ValueError):
            mod.build_record(bad, NOW)


def test_build_record_rejects_empty_or_nonlist_items():
    mod = _import()
    for bad in (
        {"goal_id": "g-1-1"},
        {"goal_id": "g-1-1", "items": []},
        {"goal_id": "g-1-1", "items": "not-a-list"},
        {"goal_id": "g-1-1", "items": ["", "   "]},
    ):
        with pytest.raises(ValueError):
            mod.build_record(bad, NOW)


def test_build_record_rejects_non_dict_payload():
    mod = _import()
    with pytest.raises(ValueError):
        mod.build_record(["not", "a", "dict"], NOW)


# ── append_record (locked JSONL append against tmp_path) ───────────────────

def test_append_record_writes_one_jsonl_line(tmp_path):
    mod = _import()
    target = tmp_path / "missing-verification-criteria.jsonl"
    rec = mod.build_record(_payload(), NOW)
    mod.append_record(rec, target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == rec


def test_append_record_appends_not_overwrites(tmp_path):
    mod = _import()
    target = tmp_path / "missing-verification-criteria.jsonl"
    mod.append_record(mod.build_record(_payload(goal_id="g-1-1"), NOW), target)
    mod.append_record(mod.build_record(_payload(goal_id="g-2-2"), NOW), target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["goal_id"] == "g-1-1"
    assert json.loads(lines[1])["goal_id"] == "g-2-2"


def test_append_record_creates_parent_dir(tmp_path):
    mod = _import()
    target = tmp_path / "nested" / "dir" / "missing-verification-criteria.jsonl"
    mod.append_record(mod.build_record(_payload(), NOW), target)
    assert target.exists()


def test_append_record_releases_lock(tmp_path):
    """The .lock sentinel must not linger after a successful append."""
    mod = _import()
    target = tmp_path / "missing-verification-criteria.jsonl"
    mod.append_record(mod.build_record(_payload(), NOW), target)
    assert not (tmp_path / "missing-verification-criteria.jsonl.lock").exists()
