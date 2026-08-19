#!/usr/bin/env python3
"""test_compact_restore_timestamp_aware.py —  regression test.

Verifies the timestamp-aware Map merge (Fix 1 of g-115-592) preserves the
newer dict when both current and checkpoint carry a timestamp/updated_at/
time field, AND preserves backward-compat checkpoint-wins behavior when
timestamps are missing or current is older.

Companion to test_compact_restore_skip_slots.py — that test exercises the
SKIP_SLOTS path (Fix 2 of g-115-593); this test exercises the merge logic
that fires for dict slots NOT in SKIP_SLOTS.

Test strategy: use a hypothetical dict slot (`hypothetical_timestamped_slot`)
not present in SKIP_SLOTS, run 3 scenarios:
  1. current newer than checkpoint  → current preserved
  2. current older than checkpoint  → checkpoint wins
  3. timestamps missing             → checkpoint wins (backward compat)

Each scenario sets up its own temp AGENT_DIR + WM + checkpoint, invokes
main(), then reads back WM and asserts.

Run: py -3 core/scripts/tests/test_compact_restore_timestamp_aware.py
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


HYP_SLOT = "hypothetical_timestamped_slot"


def _setup_temp_agent(tmpdir: Path, wm_slot_value: dict, ck_slot_value: dict):
    session = tmpdir / "session"
    session.mkdir(parents=True, exist_ok=True)

    wm_path = session / "working-memory.yaml"
    wm = {
        "version": 1,
        "agent": "test",
        "slots": {HYP_SLOT: wm_slot_value},
        "slot_meta": {},
        "goals_completed_this_session": 0,
    }
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")

    checkpoint = {
        "all_slots": {HYP_SLOT: ck_slot_value},
        "slot_meta": {},
        "goals_completed_this_session": 0,
    }
    (session / "compact-checkpoint.yaml").write_text(
        yaml.safe_dump(checkpoint, sort_keys=False), encoding="utf-8"
    )
    return wm_path


def _run_restore(tmpdir: Path):
    """Patch module bindings and invoke compact-restore-slots main().

    BODY_WM_PATH is the FIRST branch of wm.wm_path(); the AGENT_DIR patches
    below are the SECOND. On a WORKER Body the bash-agent-inject hook exports
    BODY_WM_PATH, so without pinning it every write_wm from this fixture lands
    on the LIVE per-Body working-memory.yaml (measured on the sibling
    test_compact_restore_loop_state_recovery.py, 2026-08-16, cc-08 — it wrote
    `goals_completed_this_session: 0` over the canonical LIST slot and killed
    worker-loop Phase 4b for that Body). Pinned for the duration of the call
    and restored in the finally."""
    prev_body_wm = os.environ.get("BODY_WM_PATH")
    os.environ["BODY_WM_PATH"] = str(tmpdir / "session" / "working-memory.yaml")
    os.environ["MIND_AGENT_DIR_OVERRIDE"] = str(tmpdir)

    for mod in ("_paths", "wm", "compact_restore_slots"):
        sys.modules.pop(mod, None)

    import _paths
    _paths.AGENT_DIR = tmpdir

    import wm as wm_module
    wm_module.AGENT_DIR = tmpdir
    wm_module.WORKING_MEMORY_PATH = tmpdir / "session" / "working-memory.yaml"

    spec = importlib.util.spec_from_file_location(
        "compact_restore_slots", SCRIPT_DIR / "compact-restore-slots.py"
    )
    crs_mod = importlib.util.module_from_spec(spec)
    sys.modules["compact_restore_slots"] = crs_mod
    spec.loader.exec_module(crs_mod)
    crs_mod.CHECKPOINT_PATH = tmpdir / "session" / "compact-checkpoint.yaml"

    # Sanity: HYP_SLOT must NOT be in SKIP_SLOTS, else this test is vacuous.
    assert HYP_SLOT not in crs_mod.SKIP_SLOTS, (
        f"Test invariant broken: {HYP_SLOT} is in SKIP_SLOTS — pick a different name"
    )

    try:
        crs_mod.main()
    finally:
        if prev_body_wm is None:
            os.environ.pop("BODY_WM_PATH", None)
        else:
            os.environ["BODY_WM_PATH"] = prev_body_wm


def _read_slot(wm_path: Path) -> dict:
    wm = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
    return wm.get("slots", {}).get(HYP_SLOT, {})


def case_current_newer_preserves_current() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="crs-ts-newer-"))
    try:
        wm_newer = {"timestamp": "2026-05-10T17:00:00", "counter": 270, "fresh_field": "alpha"}
        ck_older = {"timestamp": "2026-05-08T10:00:00", "counter": 100, "stale_field": "beta"}
        wm_path = _setup_temp_agent(tmpdir, wm_newer, ck_older)
        _run_restore(tmpdir)
        actual = _read_slot(wm_path)
        # Expect current preserved + null-fill from checkpoint
        if actual.get("counter") != 270:
            print(f"FAIL case_current_newer: counter clobbered: {actual.get('counter')}")
            return False
        if actual.get("timestamp") != "2026-05-10T17:00:00":
            print(f"FAIL case_current_newer: timestamp clobbered: {actual.get('timestamp')}")
            return False
        if actual.get("fresh_field") != "alpha":
            print(f"FAIL case_current_newer: fresh_field clobbered: {actual.get('fresh_field')}")
            return False
        # stale_field present in ck only — null-fill should add it
        if actual.get("stale_field") != "beta":
            print(f"FAIL case_current_newer: null-fill missed: {actual.get('stale_field')}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_current_older_applies_checkpoint() -> bool:
    tmpdir = Path(tempfile.mkdtemp(prefix="crs-ts-older-"))
    try:
        wm_older = {"timestamp": "2026-05-08T10:00:00", "counter": 100}
        ck_newer = {"timestamp": "2026-05-10T17:00:00", "counter": 270}
        wm_path = _setup_temp_agent(tmpdir, wm_older, ck_newer)
        _run_restore(tmpdir)
        actual = _read_slot(wm_path)
        # Expect checkpoint-wins
        if actual.get("counter") != 270:
            print(f"FAIL case_current_older: counter not restored: {actual.get('counter')}")
            return False
        if actual.get("timestamp") != "2026-05-10T17:00:00":
            print(f"FAIL case_current_older: timestamp not restored: {actual.get('timestamp')}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_no_timestamps_applies_checkpoint() -> bool:
    """Backward-compat: dict without timestamp falls through to old behavior."""
    tmpdir = Path(tempfile.mkdtemp(prefix="crs-ts-none-"))
    try:
        wm_no_ts = {"counter": 100, "label": "current"}
        ck_no_ts = {"counter": 270, "label": "checkpoint"}
        wm_path = _setup_temp_agent(tmpdir, wm_no_ts, ck_no_ts)
        _run_restore(tmpdir)
        actual = _read_slot(wm_path)
        # No timestamps → original checkpoint-wins behavior
        if actual.get("counter") != 270:
            print(f"FAIL case_no_timestamps: counter not restored: {actual.get('counter')}")
            return False
        if actual.get("label") != "checkpoint":
            print(f"FAIL case_no_timestamps: label not restored: {actual.get('label')}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def case_updated_at_alias() -> bool:
    """Verify updated_at field is recognized as a timestamp alias."""
    tmpdir = Path(tempfile.mkdtemp(prefix="crs-ts-alias-"))
    try:
        wm_newer = {"updated_at": "2026-05-10T17:00:00", "value": "kept"}
        ck_older = {"updated_at": "2026-05-08T10:00:00", "value": "stale"}
        wm_path = _setup_temp_agent(tmpdir, wm_newer, ck_older)
        _run_restore(tmpdir)
        actual = _read_slot(wm_path)
        if actual.get("value") != "kept":
            print(f"FAIL case_updated_at_alias: value clobbered: {actual.get('value')}")
            return False
        if actual.get("updated_at") != "2026-05-10T17:00:00":
            print(f"FAIL case_updated_at_alias: updated_at clobbered: {actual.get('updated_at')}")
            return False
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run() -> int:
    cases = [
        ("case_current_newer_preserves_current", case_current_newer_preserves_current),
        ("case_current_older_applies_checkpoint", case_current_older_applies_checkpoint),
        ("case_no_timestamps_applies_checkpoint", case_no_timestamps_applies_checkpoint),
        ("case_updated_at_alias", case_updated_at_alias),
    ]
    failures = []
    for name, fn in cases:
        ok = fn()
        if ok:
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
