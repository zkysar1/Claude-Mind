#!/usr/bin/env python3
"""test_compact_restore_skip_slots.py —  regression test.

Verifies SKIP_SLOTS expansion (Fix 2 of g-115-592 / g-115-593) prevents
the Map-merge clobber where compact-restore-slots.py L97-102 silently
overwrites newer current dict values with older checkpoint values.

Failure shape (pre-fix): cadence-stamp slots written by Phase 12 / wrappers
between checkpoint creation and restoration get reverted to the older
checkpoint snapshot, causing cadence gates to re-fire, loop_state counters
to regress, and consolidation_health to read stale.

Test strategy:
  1. Stand up a temp AGENT_DIR with a checkpoint containing OLDER dict
     values for each "latest-wins" slot.
  2. Seed working memory with NEWER values for the same slots.
  3. Invoke main() and assert each slot still contains the newer value.

Source of truth for the slot list: core/scripts/compact-restore-slots.py
SKIP_SLOTS. Test imports the constant directly so additions automatically
get coverage.

Run: py -3 core/scripts/tests/test_compact_restore_skip_slots.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


CHECKPOINT_OLDER = {
    # cadence-stamp dict slots — checkpoint snapshot is older
    "last_felt_sense_checkin": {"timestamp": "2026-05-08T10:00:00", "goals_count_at_last_fire": 100},
    "last_fresh_eyes_review": {"timestamp": "2026-05-08T10:00:00", "goals_count_at_last_fire": 100},
    "last_fresh_eyes_program_review": {"timestamp": "2026-05-08T10:00:00", "goals_count_at_last_fire": 100},
    "last_strategic_scan": {"timestamp": "2026-05-08T10:00:00", "goals_count_at_last_fire": 100},
    # iteration-state dict slots — checkpoint snapshot is older
    "loop_state": {"goals_completed": 1, "productive_goals": 1, "evolutions": 0},
    "consolidation_health": {"avg_completion": 0.30, "near_complete": 1, "stalled": 0},
    "portfolio_health_signal": {"last_detected_at": "2026-05-08T10:00:00", "frontier_thin": False},
}

WM_NEWER = {
    "last_felt_sense_checkin": {"timestamp": "2026-05-10T17:00:00", "goals_count_at_last_fire": 270},
    "last_fresh_eyes_review": {"timestamp": "2026-05-10T17:00:00", "goals_count_at_last_fire": 270},
    "last_fresh_eyes_program_review": {"timestamp": "2026-05-10T17:00:00", "goals_count_at_last_fire": 270},
    "last_strategic_scan": {"timestamp": "2026-05-10T17:00:00", "goals_count_at_last_fire": 270},
    "loop_state": {"goals_completed": 27, "productive_goals": 24, "evolutions": 0},
    "consolidation_health": {"avg_completion": 0.61, "near_complete": 14, "stalled": 0},
    "portfolio_health_signal": {"last_detected_at": "2026-05-10T17:00:00", "frontier_thin": True},
}


def _setup_temp_agent(tmpdir: Path):
    """Build a temp AGENT_DIR with session/ + working-memory.yaml + checkpoint."""
    session = tmpdir / "session"
    session.mkdir(parents=True, exist_ok=True)

    wm_path = session / "working-memory.yaml"
    wm = {
        "version": 1,
        "agent": "test",
        "slots": dict(WM_NEWER),
        "slot_meta": {},
        "goals_completed_this_session": 27,
    }
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")

    checkpoint = {
        "all_slots": dict(CHECKPOINT_OLDER),
        "slot_meta": {},
        "goals_completed_this_session": 1,
    }
    (session / "compact-checkpoint.yaml").write_text(
        yaml.safe_dump(checkpoint, sort_keys=False), encoding="utf-8"
    )
    return wm_path


def run() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="compact-restore-test-"))
    try:
        wm_path = _setup_temp_agent(tmpdir)

        # Patch AGENT_DIR before importing the module so its module-level
        # CHECKPOINT_PATH binding picks up the temp location.
        os.environ["MIND_AGENT_DIR_OVERRIDE"] = str(tmpdir)
        # _paths.py reads MIND_AGENT to resolve AGENT_DIR; fast path is to
        # monkey-patch the module's AGENT_DIR after import. Do that directly.

        import importlib

        # Force reimport in case earlier tests cached _paths
        for mod in ("_paths", "wm", "compact_restore_slots"):
            sys.modules.pop(mod, None)

        # Import _paths and override AGENT_DIR before compact-restore-slots
        # picks it up.
        import _paths
        _paths.AGENT_DIR = tmpdir

        # wm.py uses _paths.AGENT_DIR via from-import — reimport so it
        # captures our patched value, then patch wm's local binding too.
        import wm as wm_module
        wm_module.AGENT_DIR = tmpdir
        wm_module.WORKING_MEMORY_PATH = tmpdir / "session" / "working-memory.yaml"

        # Now load compact-restore-slots (filename has hyphens — use spec_from_file_location)
        spec = importlib.util.spec_from_file_location(
            "compact_restore_slots",
            SCRIPT_DIR / "compact-restore-slots.py"
        )
        crs_mod = importlib.util.module_from_spec(spec)
        sys.modules["compact_restore_slots"] = crs_mod
        spec.loader.exec_module(crs_mod)
        crs_mod.CHECKPOINT_PATH = tmpdir / "session" / "compact-checkpoint.yaml"

        # Confirm the SKIP_SLOTS constant has the expanded set
        expected_skip = {
            "archived_context",
            "last_felt_sense_checkin",
            "last_fresh_eyes_review",
            "last_fresh_eyes_program_review",
            "last_strategic_scan",
            "loop_state",
            "consolidation_health",
            "portfolio_health_signal",
        }
        assert expected_skip.issubset(crs_mod.SKIP_SLOTS), (
            f"SKIP_SLOTS missing entries: "
            f"{expected_skip - crs_mod.SKIP_SLOTS}"
        )
        print(f"PASS: SKIP_SLOTS contains all {len(expected_skip)} expected slots")

        # Run the restore — should leave WM newer values intact
        crs_mod.main()

        # Read WM back and verify newer values survived
        wm_after = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        slots = wm_after.get("slots", {})

        failures = []
        for slot, newer in WM_NEWER.items():
            actual = slots.get(slot)
            if actual != newer:
                failures.append(
                    f"  {slot}: expected {newer} but got {actual}"
                )

        if failures:
            print("FAIL: Map-merge clobber NOT prevented for some slots:")
            for f in failures:
                print(f)
            return 1

        print(
            f"PASS: All {len(WM_NEWER)} latest-wins dict slots preserved their newer "
            "values (Map merge bypassed via SKIP_SLOTS)"
        )
        return 0

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
