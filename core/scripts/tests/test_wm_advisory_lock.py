"""Stress test for the working-memory.yaml advisory lock ().

Spawns N concurrent writers, each updating a DISJOINT slot via wm-set.sh.
Without the lock, two writers can read the same baseline, mutate, and the
second commit clobbers the first. With the lock, every update lands.

Verification: after all writers finish, every disjoint slot must hold its
expected value. Any missing slot indicates a clobber (the lock leaked).

Run:
  py -3 core/scripts/tests/test_wm_advisory_lock.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
_SD = CORE_ROOT / "scripts"
if str(_SD) not in sys.path:
    sys.path.insert(0, str(_SD))

from _paths import agent_dir  # noqa: E402

# OFF-ROSTER BY CONSTRUCTION (). This bound `MIND_AGENT` from the
# AMBIENT environment with a `"bravo"` fallback, so under the suite it wrote the
# working memory of whichever LIVE agent was running — measured on alpha/cc-04,
# where lock_stress_a/b/c sat in the live file for two days with update_count
# 3/4/1 across separate suite runs. The `clear` at the end of main() is not a
# defence: it leaves slot_meta behind, and any failed or interrupted run skips it
# entirely. The lock semantics under test are unchanged — still the agent-wide
# WM, still disjoint slots, still a concurrent burst — only the victim moves off
# the live roster, which is the remedy check-tests-no-live-agent-wm.py names.
TEST_AGENT = "testagent"

WM_PATH = agent_dir(TEST_AGENT) / "session" / "working-memory.yaml"


WM_PY = _SD / "wm.py"


def _env() -> dict:
    env = os.environ.copy()
    # NOT os.environ.get(..., default): inheriting the ambient binding is the
    # defect. Bind unconditionally so the writer subprocesses and the daemon
    # reader below both resolve the same off-roster agent.
    env["MIND_AGENT"] = TEST_AGENT
    # guard-862 / guard-3375 (): on a worker Body the inherited
    # BODY_WM_PATH routes the wm.py WRITER subprocesses to the per-Body WM
    # while the daemon READER (_rt.wm_read) resolves the agent-wide WM — so
    # every read-back was null (measured cc-07 2026-08-17). Drop it so both
    # sides target the agent-wide WM, which this test already exercises
    # safely via disjoint lock_stress_* slots.
    env.pop("BODY_WM_PATH", None)
    return env


def write_slot(slot: str, value: str) -> tuple[int, str]:
    """Spawn wm.py set as a subprocess. Returns (returncode, stderr).

    Calls wm.py directly (not via wm-set.sh) — bash invocation from
    subprocess on Windows mangles paths and the wrapper script just
    delegates to this same wm.py command anyway.
    """
    cmd = [sys.executable, str(WM_PY), "set", slot]
    proc = subprocess.run(
        cmd,
        input=f'"{value}"',
        text=True,
        capture_output=True,
        env=_env(),
        timeout=30,
    )
    return proc.returncode, proc.stderr


def read_slot(slot: str) -> str:
    """Read a WM slot straight from working-memory.yaml, the file the writers contend on.

    This went through the daemon (_rt.wm_read) until g-358-244. The lock under test
    protects that FILE's read-modify-write, so the file is what to read back; the
    daemon route added a second precondition (a live daemon resolving the same
    off-roster agent) that a daemon-quiesced deployment does not meet (omni-382
    class 6).
    """
    import yaml

    data = yaml.safe_load(WM_PATH.read_text(encoding="utf-8")) or {}
    got = (data.get("slots") or {}).get(slot)
    return "" if got is None else str(got).strip().strip('"')


def main() -> int:
    # `wm.py set` creates a missing working-memory.yaml itself (), so an
    # absent file is not a precondition failure. The guard that stood here returned
    # rc=2 "not initialized" on every checkout that never ran wm-init for the
    # off-roster agent: a fresh clone, a worktree, a deployment (omni-382, ).
    created = not WM_PATH.exists()

    # Use 3 disjoint test slots to avoid corrupting any real slot. They are
    # set as top-level via wm-set.sh's slot routing — wm.py auto-creates
    # paths under slots:.
    slots = {
        "lock_stress_a": "value_from_writer_a",
        "lock_stress_b": "value_from_writer_b",
        "lock_stress_c": "value_from_writer_c",
    }

    # Concurrent burst: spawn all writers at the same moment using ProcessPoolExecutor
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"[test] firing {len(slots)} concurrent writers...")
    t0 = time.time()
    failures = []
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        futures = {pool.submit(write_slot, s, v): s for s, v in slots.items()}
        for fut in as_completed(futures):
            slot = futures[fut]
            try:
                rc, err = fut.result()
                if rc != 0:
                    failures.append(f"{slot}: rc={rc} stderr={err.strip()[:200]}")
            except Exception as e:
                failures.append(f"{slot}: exception {e}")
    elapsed = time.time() - t0
    print(f"[test] all writers done in {elapsed:.2f}s")

    if failures:
        print("FAIL: writer failures:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1

    # Verify each slot's final value matches what its writer set. A clobber
    # would leave one of the slots null or holding a stale value.
    print("[test] reading back...")
    missing = []
    for slot, expected in slots.items():
        got = read_slot(slot)
        if got != expected:
            missing.append(f"{slot}: expected={expected!r} got={got!r}")

    # Cleanup: clear test slots so we don't pollute working memory.
    for slot in slots:
        subprocess.run(
            [sys.executable, str(WM_PY), "clear", slot],
            env=_env(),
            capture_output=True,
            timeout=10,
        )
    if created:
        # No residue: the file and its lock exist only because this run made them.
        for leftover in (WM_PATH, WM_PATH.with_suffix(".lock")):
            leftover.unlink(missing_ok=True)

    if missing:
        print("FAIL: clobber detected — final state does not contain all updates:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 1

    print(f"PASS: all {len(slots)} disjoint writes landed; lock prevents clobber")
    return 0


if __name__ == "__main__":
    sys.exit(main())
