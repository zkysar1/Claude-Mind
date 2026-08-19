#!/usr/bin/env python3
"""test_compact_restore_loop_state_recovery.py —  regression test.

Verifies the null-guarded loop_state recovery added to
compact-restore-slots.py (`_recover_lost_loop_state`).

Background (g-115-1302): loop_state is in SKIP_SLOTS (the general merge never
restores it) AND the freshness gate (g-115-684) skips the whole restore when
wm.yaml mtime > checkpoint mtime. Neither path recovers a loop_state that was
NULLED/LOST in the compaction window. The verified failure shape: loop_state
valid at PreCompact (the checkpoint snapshot captured goals_completed=3), null
on disk after compaction, and compact-restore refused to recover it — so
loop-state-bump-counters.py self-init'd from goals_completed=0 (g-115-622),
silently resetting the session count.

Two invariants, both tested:
  A. RECOVER when on-disk loop_state is null/lost AND the checkpoint carries a
     valid loop_state dict → loop_state is restored from the checkpoint.
  B. NEVER CLOBBER a valid on-disk loop_state with the checkpoint value
     (preserves the g-115-593 latest-wins intent that put loop_state in
     SKIP_SLOTS). This complements test_compact_restore_skip_slots.py.

Run: py -3 core/scripts/tests/test_compact_restore_loop_state_recovery.py
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


def _pin_body_wm(tmpdir: Path):
    """Pin wm_path() to the fixture file via BODY_WM_PATH — the FIRST branch of
    wm.wm_path(). Patching AGENT_DIR (below) is the SECOND branch and holds only
    where BODY_WM_PATH is unset. On a WORKER Body the bash-agent-inject hook
    exports BODY_WM_PATH into every call, so without this pin every write_wm
    from this fixture landed on the LIVE per-Body working-memory.yaml —
    measured 2026-08-16 (alpha worker, cc-08): the fixture's
    `goals_completed_this_session: 0` overwrote the canonical top-level LIST
    slot with an int, `wm-append.sh goals_completed_this_session` then refused
    every Phase 4b hand-off row, and body-merge's list/int type-mismatch branch
    silently dropped the Body's contribution (msg-20260816-183354-alpha-5099;
    conftest rule: tests that override the WM path MUST set BODY_WM_PATH, never
    patch AGENT_DIR alone). The only environment where the old isolation failed
    was the only one where it mattered. Returns the prior value for restore."""
    prev = os.environ.get("BODY_WM_PATH")
    os.environ["BODY_WM_PATH"] = str(tmpdir / "session" / "working-memory.yaml")
    return prev


def _unpin_body_wm(prev):
    if prev is None:
        os.environ.pop("BODY_WM_PATH", None)
    else:
        os.environ["BODY_WM_PATH"] = prev


def _load_crs_module(tmpdir: Path):
    """Import compact-restore-slots against a temp AGENT_DIR (mirrors the
    proven harness in test_compact_restore_skip_slots.py). Callers MUST have
    pinned BODY_WM_PATH first (_pin_body_wm) — see that helper for why."""
    for mod in ("_paths", "wm", "compact_restore_slots"):
        sys.modules.pop(mod, None)

    import _paths
    _paths.AGENT_DIR = tmpdir

    import wm as wm_module
    # AGENT_DIR is the load-bearing isolation: wm_path() resolves BODY_WM_PATH
    # env → else AGENT_DIR/session/working-memory.yaml, and read_wm/write_wm
    # call wm_path() directly. Patching wm.WM_PATH / WORKING_MEMORY_PATH was a
    # post- NO-OP for I/O (WM_PATH became a dynamic __getattr__
    # property) and contradicted the conftest guidance — removed. ()
    wm_module.AGENT_DIR = tmpdir

    spec = importlib.util.spec_from_file_location(
        "compact_restore_slots", SCRIPT_DIR / "compact-restore-slots.py"
    )
    crs_mod = importlib.util.module_from_spec(spec)
    sys.modules["compact_restore_slots"] = crs_mod
    spec.loader.exec_module(crs_mod)
    crs_mod.CHECKPOINT_PATH = tmpdir / "session" / "compact-checkpoint.yaml"
    crs_mod.WM_PATH = tmpdir / "session" / "working-memory.yaml"
    return crs_mod


def _write_state(tmpdir: Path, disk_loop_state, ck_loop_state):
    """Write wm.yaml (disk loop_state) then checkpoint (ck loop_state)."""
    session = tmpdir / "session"
    session.mkdir(parents=True, exist_ok=True)
    wm_path = session / "working-memory.yaml"
    wm = {
        "version": 1,
        "agent": "test",
        "slots": {"loop_state": disk_loop_state, "active_strategy": "x"},
        "slot_meta": {},
        "goals_completed_this_session": 0,
    }
    wm_path.write_text(yaml.safe_dump(wm, sort_keys=False), encoding="utf-8")
    checkpoint = {
        "all_slots": {"loop_state": ck_loop_state, "active_strategy": "x"},
        "slot_meta": {},
        "goals_completed_this_session": 0,
    }
    (session / "compact-checkpoint.yaml").write_text(
        yaml.safe_dump(checkpoint, sort_keys=False), encoding="utf-8"
    )
    return wm_path


VALID_CK = {"goals_completed": 3, "productive_goals": 2, "evolutions": 0,
            "signals": {"routine_streak_global": 0}}


def _case_recover_when_null() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="crs-recover-null-"))
    prev_body_wm = _pin_body_wm(tmpdir)
    try:
        wm_path = _write_state(tmpdir, disk_loop_state=None, ck_loop_state=VALID_CK)
        crs = _load_crs_module(tmpdir)
        crs.main()
        wm_after = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        ls = (wm_after.get("slots") or {}).get("loop_state")
        if not isinstance(ls, dict) or ls.get("goals_completed") != 3:
            print(f"FAIL[A recover-when-null]: loop_state not recovered — got {ls}")
            return 1
        print("PASS[A recover-when-null]: null on-disk loop_state recovered "
              "from checkpoint (goals_completed=3)")
        return 0
    finally:
        _unpin_body_wm(prev_body_wm)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _case_never_clobber_valid() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="crs-no-clobber-"))
    prev_body_wm = _pin_body_wm(tmpdir)
    try:
        disk_valid = {"goals_completed": 5, "productive_goals": 4, "evolutions": 1}
        ck_older = {"goals_completed": 1, "productive_goals": 1, "evolutions": 0}
        wm_path = _write_state(tmpdir, disk_loop_state=disk_valid, ck_loop_state=ck_older)
        crs = _load_crs_module(tmpdir)
        crs.main()
        wm_after = yaml.safe_load(wm_path.read_text(encoding="utf-8")) or {}
        ls = (wm_after.get("slots") or {}).get("loop_state")
        if not isinstance(ls, dict) or ls.get("goals_completed") != 5:
            print(f"FAIL[B never-clobber]: valid on-disk loop_state was "
                  f"clobbered — expected goals_completed=5, got {ls}")
            return 1
        print("PASS[B never-clobber]: valid on-disk loop_state (goals_completed=5) "
              "preserved against older checkpoint (g-115-593 intent intact)")
        return 0
    finally:
        _unpin_body_wm(prev_body_wm)
        shutil.rmtree(tmpdir, ignore_errors=True)


def run() -> int:
    rc = 0
    rc |= _case_recover_when_null()
    rc |= _case_never_clobber_valid()
    if rc == 0:
        print("ALL PASS: loop_state recovery is null-guarded (recovers lost, "
              "never clobbers valid)")
    return rc


def test_loop_state_recovery():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
