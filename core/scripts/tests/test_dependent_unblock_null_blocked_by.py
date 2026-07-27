"""Regression test — a goal carrying `blocked_by: null` must not crash _scan.

THE DEFECT (found 2026-07-26 during g-306-93). `_scan` read the field as
`goal.get("blocked_by", [])`. That default fires only when the KEY IS ABSENT;
a goal carrying the key with an explicit null returns None, and the very next
line — `if completed_id in bb` — raised
`TypeError: argument of type 'NoneType' is not iterable`.

BLAST RADIUS. `_scan` walks EVERY goal in both queues on EVERY goal
completion, and aborts on the first bad record. So ONE such goal disables
dependent-unblocking for the whole fleet. Exactly one existed (g-350-10,
written 2026-07-15T13:27:41) out of 3344 goals, and it silently broke every
close for the following 11 days: no dependent had its `blocked_by` cleared and
no predecessor output was injected during that window.

WHY IT SURVIVED. The two sibling test files
(test_dependent_unblock_status_restore.py, test_dependent_unblock_windows_path.py)
both `mock.patch.object(mod, "_scan", ...)`, so neither ever executed the real
scan. This test drives `_scan` itself against a tmp queue — the layer the
mocks skip.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent

if not os.environ.get("MIND_AGENT"):
    os.environ["MIND_AGENT"] = "alpha"


def _load():
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "dependent_unblock_nullbb", str(SCRIPTS_DIR / "dependent-unblock.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(tmp_path, goals):
    (tmp_path / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-1", "goals": goals}) + "\n", encoding="utf-8")
    return tmp_path


def _scan_with(monkeypatch, tmp_path, goals, completed="g-1-1"):
    mod = _load()
    monkeypatch.setattr(mod, "WORLD_DIR", _seed(tmp_path, goals))
    monkeypatch.setattr(mod, "AGENT_DIR", None)
    return mod._scan(completed)


def test_null_blocked_by_does_not_crash(monkeypatch, tmp_path):
    """The exact live shape: an explicit null alongside a real dependent."""
    blocked, deps = _scan_with(monkeypatch, tmp_path, [
        {"id": "g-1-2", "blocked_by": None},          # the poison record
        {"id": "g-1-3", "blocked_by": ["g-1-1"]},     # must still be found
    ])
    assert [g["id"] for _s, _a, g in blocked] == ["g-1-3"], (
        "a goal with blocked_by:null must be skipped, and must not prevent "
        "later goals in the same queue from being scanned"
    )
    assert deps == []


def test_null_record_does_not_mask_later_matches(monkeypatch, tmp_path):
    """Ordering guard: the null must not abort the walk before real matches.

    This is the property that actually failed in production — the crash killed
    the whole scan, so every goal after the bad record became invisible.
    """
    goals = [{"id": "g-1-0", "blocked_by": None}]
    goals += [{"id": f"g-1-{i}", "blocked_by": ["g-1-1"]} for i in range(2, 6)]
    blocked, _ = _scan_with(monkeypatch, tmp_path, goals)
    assert [g["id"] for _s, _a, g in blocked] == ["g-1-2", "g-1-3", "g-1-4", "g-1-5"]


def test_absent_missing_and_string_forms_still_work(monkeypatch, tmp_path):
    """The pre-existing accepted shapes are unchanged by the null fix."""
    blocked, _ = _scan_with(monkeypatch, tmp_path, [
        {"id": "g-1-2"},                              # key absent
        {"id": "g-1-3", "blocked_by": []},            # empty list
        {"id": "g-1-4", "blocked_by": "g-1-1"},       # bare string
        {"id": "g-1-5", "blocked_by": ["g-9-9"]},     # unrelated blocker
    ])
    assert [g["id"] for _s, _a, g in blocked] == ["g-1-4"]


def test_null_depends_on_is_also_safe(monkeypatch, tmp_path):
    """Sibling field: depends_on:null is guarded by isinstance, pin that too."""
    _blocked, deps = _scan_with(monkeypatch, tmp_path, [
        {"id": "g-1-2", "depends_on": None},
        {"id": "g-1-3", "depends_on": ["g-1-1"]},
    ])
    assert [g["id"] for _s, _a, g in deps] == ["g-1-3"]
