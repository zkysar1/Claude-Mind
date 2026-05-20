"""Unit test for cmd_reset cadence-tracker preservation ().

cmd_reset historically only preserved SESSION_IDENTITY_FIELDS = {session_start},
nulling cadence-tracker slots like last_felt_sense_checkin. g-115-318
investigation confirmed this caused duplicate firings of cadence gates
after consolidate Step 5 wm-reset (the same gap appeared overdue twice).

This test verifies the fix: cadence-tracker slots (matching the patterns in
CADENCE_TRACKER_PATTERNS) survive cmd_reset; non-cadence slots are nulled.

Runs against an isolated temp-dir WM file via module-level path patching.
Does NOT touch live agent working memory.

Run:
  py -3 core/scripts/tests/test_wm_reset_cadence.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

import wm  # noqa: E402


def main() -> int:
    failures = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # Redirect WM file paths to temp dir for the duration of the test.
        original_wm_path = wm.WM_PATH
        original_lock_path = wm.WM_LOCK_PATH
        wm.WM_PATH = tmp / "working-memory.yaml"
        wm.WM_LOCK_PATH = wm.WM_PATH.with_suffix(".lock")
        try:
            # 1. Initialize a fresh WM via cmd_init
            init_args = SimpleNamespace()
            wm.cmd_init(init_args)
            assert wm.WM_PATH.exists(), "init failed to create WM file"

            # 2. Seed cadence-tracker + non-cadence slots with known values.
            #    The cadence-tracker patterns require the slot name to start
            #    with "last_" and end with one of: _tick, _check, _checkin,
            #    _scan, _fire.
            cadence_slots = {
                "last_test_tick": "2026-04-30T01:00:00",
                "last_test_check": "2026-04-30T01:01:00",
                "last_test_checkin": "2026-04-30T01:02:00",
                "last_test_scan": "2026-04-30T01:03:00",
                "last_test_fire": "2026-04-30T01:04:00",
            }
            # Only use slots not in slot_types so we test the dynamically-created
            # non-cadence-slot path. (Slots in slot_types reset to their defaults:
            # None for scalars, empty dict for MAP_SLOTS, [] for ARRAY_SLOTS.
            # Verifying default-reset is wm-init's territory, not this test's.)
            non_cadence_slots = {
                "test_canary": "this_should_disappear",
                "test_marker_a": "remove_me",
            }

            data = wm.read_wm()
            for k, v in {**cadence_slots, **non_cadence_slots}.items():
                data["slots"][k] = v
                data["slot_meta"][k] = {
                    "updated_at": "2026-04-30T01:00:00",
                    "accessed_at": "2026-04-30T01:00:00",
                    "update_count": 1,
                }
            # Also seed a session-identity field
            data["session_start"] = "2026-04-30T00:00:00"
            wm.write_wm(data)

            # Sanity: verify seeds landed
            seeded = wm.read_wm()
            for k in cadence_slots:
                if seeded["slots"].get(k) != cadence_slots[k]:
                    failures.append(f"seed failed: {k}")
            for k in non_cadence_slots:
                if seeded["slots"].get(k) != non_cadence_slots[k]:
                    failures.append(f"seed failed: {k}")

            # 3. Call cmd_reset
            reset_args = SimpleNamespace()
            wm.cmd_reset(reset_args)

            # 4. Verify cadence-tracker slots survived
            after = wm.read_wm()
            for k, expected in cadence_slots.items():
                actual = after["slots"].get(k)
                if actual != expected:
                    failures.append(
                        f"PRESERVE FAIL: cadence slot {k} expected {expected!r}, got {actual!r}"
                    )
                # Verify slot_meta also survived for cadence trackers
                meta = after.get("slot_meta", {}).get(k)
                if not meta or meta.get("update_count") != 1:
                    failures.append(
                        f"PRESERVE FAIL: cadence slot_meta {k} not preserved (got {meta!r})"
                    )

            # 5. Verify non-cadence slots were nulled (or removed for test_canary
            #    which isn't in slot_types)
            for k in non_cadence_slots:
                actual = after["slots"].get(k)
                if actual is not None:
                    failures.append(
                        f"NULL FAIL: non-cadence slot {k} should be None, got {actual!r}"
                    )

            # 6. Verify session_start was preserved (SESSION_IDENTITY_FIELDS)
            if after.get("session_start") != "2026-04-30T00:00:00":
                failures.append(
                    f"IDENTITY FAIL: session_start should be preserved, got {after.get('session_start')!r}"
                )

        finally:
            wm.WM_PATH = original_wm_path
            wm.WM_LOCK_PATH = original_lock_path

    if failures:
        print("[test] FAIL — cmd_reset cadence-tracker preservation broken")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("[test] PASS — cmd_reset preserves cadence-tracker slots and session_start; nulls non-cadence slots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
