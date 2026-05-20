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
import _rt  # canonical Python -> daemon client (post-cutover)

from _paths import AGENT_DIR  # noqa: E402

WM_PATH = AGENT_DIR / "session" / "working-memory.yaml"


WM_PY = _SD / "wm.py"


def _env() -> dict:
    env = os.environ.copy()
    env["MIND_AGENT"] = os.environ.get("MIND_AGENT", "bravo")
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
    """Read a WM slot via the daemon (_rt.wm_read).

    The wm.py read CLI subcommand was deleted in the 2026-05-14 daemon
    cutover; _rt.wm_read is the canonical Python->daemon replacement.
    Env is set before the call so the daemon resolves the same agent.
    """
    env = _env()
    prev = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        body = _rt.wm_read(slot=slot, as_json=False)
        return body.strip().strip('"')
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    if not WM_PATH.exists():
        print(f"FAIL: working memory not initialized at {WM_PATH}", file=sys.stderr)
        return 2

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

    if missing:
        print("FAIL: clobber detected — final state does not contain all updates:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 1

    print(f"PASS: all {len(slots)} disjoint writes landed; lock prevents clobber")
    return 0


if __name__ == "__main__":
    sys.exit(main())
