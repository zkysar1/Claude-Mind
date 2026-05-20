#!/usr/bin/env python3
"""test_override_helpers_gate_ids.py —  regression test.

Verifies audit_bulk_override() writes a `gate_ids` field translating the
slot list into canonical gate ids (from core/config/gates.yaml). Closes
the schema gap where ledger entries had only `slots_filled` (argparse
dest names) — downstream consumers wanting to pivot by gate fell back
to "unknown" because the canonical id wasn't recorded.

Test strategy: redirect WORLD_DIR to a temp dir, invoke
audit_bulk_override with controlled slots, read back the JSONL record,
assert the record's `gate_ids` matches the expected mapping.

Cases:
  1. All slots mapped → gate_ids populated, no gate_ids_unmapped key.
  2. Mixed mapped + unmapped slot → gate_ids has the mapped ones,
     gate_ids_unmapped has the unmapped ones.
  3. Empty slots_filled → no record written (early return path).

Run: py -3 core/scripts/tests/test_override_helpers_gate_ids.py
"""
from __future__ import annotations

import importlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _setup_world(tmpdir: Path):
    """Patch _paths.WORLD_DIR to tmpdir and reload the override-helpers module."""
    # _paths reads from local-paths.conf; patch its WORLD_DIR after import
    for mod in ("_paths", "_fileops", "_override_helpers"):
        sys.modules.pop(mod, None)
    import _paths
    _paths.WORLD_DIR = tmpdir
    import _override_helpers
    _override_helpers.WORLD_DIR = tmpdir  # also patch the binding the module captured
    return _override_helpers


def _read_ledger(tmpdir: Path):
    p = tmpdir / "override-bypass-ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def case_all_slots_mapped() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="oh-all-mapped-"))
    try:
        mod = _setup_world(tmpdir)
        mod.audit_bulk_override(
            token="t1",
            justification="all-mapped smoke",
            slots_filled=["override_signal", "override_duplication", "override_uncommitted"],
            context={"caller": "test", "goal_id": "g-test-1"},
        )
        records = _read_ledger(tmpdir)
        if len(records) != 1:
            print(f"FAIL case_all_slots_mapped: expected 1 record, got {len(records)}")
            return False
        rec = records[0]
        expected_ids = ["origin-signal-gate", "goal-duplication-gate", "uncommitted-work-gate"]
        if rec.get("gate_ids") != expected_ids:
            print(f"FAIL case_all_slots_mapped: gate_ids mismatch: {rec.get('gate_ids')}")
            return False
        if "gate_ids_unmapped" in rec:
            print(f"FAIL case_all_slots_mapped: gate_ids_unmapped should be absent when all mapped")
            return False
        if rec.get("slots_filled") != ["override_signal", "override_duplication", "override_uncommitted"]:
            print(f"FAIL case_all_slots_mapped: slots_filled not preserved")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_mixed_mapped_unmapped() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="oh-mixed-"))
    try:
        mod = _setup_world(tmpdir)
        mod.audit_bulk_override(
            token="t2",
            justification="mixed-mapped smoke",
            slots_filled=["override_signal", "override_future_unknown_slot"],
            context={"caller": "test"},
        )
        records = _read_ledger(tmpdir)
        if len(records) != 1:
            print(f"FAIL case_mixed: expected 1 record, got {len(records)}")
            return False
        rec = records[0]
        if rec.get("gate_ids") != ["origin-signal-gate"]:
            print(f"FAIL case_mixed: gate_ids should have only origin-signal-gate: {rec.get('gate_ids')}")
            return False
        if rec.get("gate_ids_unmapped") != ["override_future_unknown_slot"]:
            print(f"FAIL case_mixed: gate_ids_unmapped: {rec.get('gate_ids_unmapped')}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_empty_slots_no_write() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="oh-empty-"))
    try:
        mod = _setup_world(tmpdir)
        mod.audit_bulk_override(
            token="t3",
            justification="empty slots — should noop",
            slots_filled=[],
            context={"caller": "test"},
        )
        records = _read_ledger(tmpdir)
        if records:
            print(f"FAIL case_empty: expected no record, got {len(records)}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_no_token_no_write() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="oh-notoken-"))
    try:
        mod = _setup_world(tmpdir)
        mod.audit_bulk_override(
            token=None,
            justification="no token",
            slots_filled=["override_signal"],
            context={"caller": "test"},
        )
        records = _read_ledger(tmpdir)
        if records:
            print(f"FAIL case_no_token: expected no record, got {len(records)}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run() -> int:
    cases = [
        ("case_all_slots_mapped", case_all_slots_mapped),
        ("case_mixed_mapped_unmapped", case_mixed_mapped_unmapped),
        ("case_empty_slots_no_write", case_empty_slots_no_write),
        ("case_no_token_no_write", case_no_token_no_write),
    ]
    failures = []
    for name, fn in cases:
        if fn():
            print(f"  PASS  {name}")
        else:
            failures.append(name)
            print(f"  FAIL  {name}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print(f"\nAll {len(cases)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
