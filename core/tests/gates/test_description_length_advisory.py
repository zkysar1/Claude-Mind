"""Behavior tests for description_length advisory (PR 7c/5).

Advisory — never blocks. Returns a dict the caller renders. Telemetry
append is best-effort.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.description_length import evaluate, DESCRIPTION_LENGTH_MIN_CHARS


def test_short_description_warns():
    out = evaluate({"description": "too short"}, source="agent")
    assert out["warned"] is True
    assert "description short" in out["message"]
    assert out["description_length"] == len("too short")


def test_long_description_does_not_warn():
    desc = "x" * (DESCRIPTION_LENGTH_MIN_CHARS + 1)
    out = evaluate({"description": desc}, source="agent")
    assert out["warned"] is False
    assert out["message"] is None


def test_exact_threshold_does_not_warn():
    """description_length == threshold → not warned (inclusive >=)."""
    desc = "x" * DESCRIPTION_LENGTH_MIN_CHARS
    out = evaluate({"description": desc}, source="agent")
    assert out["warned"] is False


def test_recurring_goal_exempt():
    """Recurring goals are title-as-spec — never warned regardless of length."""
    out = evaluate(
        {"description": "x", "recurring": True},
        source="agent",
    )
    assert out["warned"] is False
    assert out["_reason"] == "recurring goals exempt"


def test_empty_description_warns():
    out = evaluate({}, source="agent")
    assert out["warned"] is True
    assert out["description_length"] == 0


def test_whitespace_only_description_warns():
    """Description that's just whitespace strips to empty → warns."""
    out = evaluate({"description": "      \n  \t  "}, source="agent")
    assert out["warned"] is True
    assert out["description_length"] == 0


def test_no_meta_dir_skips_telemetry(tmp_path):
    out = evaluate({"description": "short"}, source="agent")
    assert out["warned"] is True
    assert out["telemetry_written"] is False


def test_telemetry_record_written(tmp_path):
    meta = tmp_path / "meta"
    meta.mkdir()
    out = evaluate(
        {"id": "g-001-99", "description": "tiny", "defer_reason": "blocked"},
        source="agent",
        meta_dir=meta,
    )
    assert out["warned"] is True
    assert out["telemetry_written"] is True
    log = meta / "description-length-telemetry.jsonl"
    assert log.exists()
    entries = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l]
    assert len(entries) == 1
    rec = entries[0]
    assert rec["goal_id"] == "g-001-99"
    assert rec["len"] == 4
    assert rec["recurring"] is False
    assert rec["defer_set"] is True
    assert rec["source"] == "agent"
    assert rec["decision"] == "warn"


def test_telemetry_failure_does_not_break_warning(tmp_path):
    """Pass a non-writable meta_dir (a file, not a directory) — telemetry
    should fail silently and the warning should still fire."""
    bad_meta = tmp_path / "not-a-dir"
    bad_meta.write_text("file, not a dir", encoding="utf-8")
    out = evaluate(
        {"description": "short"},
        source="agent",
        meta_dir=bad_meta,
    )
    assert out["warned"] is True
    assert out["telemetry_written"] is False  # gracefully failed
    assert out["message"] is not None


def test_auto_assigned_id_placeholder(tmp_path):
    """Goal without id → telemetry records '<auto-assigned>'."""
    meta = tmp_path / "meta"
    meta.mkdir()
    evaluate({"description": "short"}, source="agent", meta_dir=meta)
    log = meta / "description-length-telemetry.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["goal_id"] == "<auto-assigned>"
