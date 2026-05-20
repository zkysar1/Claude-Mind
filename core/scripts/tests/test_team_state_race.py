"""test_team_state_race.py — concurrency smoke test for .

Verifies that concurrent writers targeting DIFFERENT rows of team-state.yaml
all survive. Before locked_modify_yaml (g-240-52), cmd_in_flight did an
unlocked read → modify → locked_write: two agents racing could both read
the same baseline and the second writer's row would silently clobber the
first's. This test catches regressions of that pattern.

The test spawns N=8 concurrent subprocesses, each calling
`team-state.py in-flight --agent test-race-N --goal-id g-race-N --title ...
--phase execute`. After all complete, the test reads team-state.yaml and
asserts every test-race-N row is present in agent_status with in_flight
set. Pass: all 8 rows survive. Fail: any row missing (clobbered).

The test backs up the live team-state.yaml, seeds an empty one, runs the
race, asserts, then restores the backup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import WORLD_DIR  # type: ignore

TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"
BACKUP_PATH = TEAM_STATE_PATH.with_suffix(f".yaml.race-test-backup.{os.getpid()}")
N_RACERS = 8
TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"


def run_in_flight(agent_name: str) -> subprocess.CompletedProcess:
    """Invoke team-state.py in-flight for one racer. Returns CompletedProcess.

    Passes MIND_WORLD explicitly so the subprocess resolves the same
    team-state.yaml as the test. Without this, MIND_AGENT=test-race-N
    routes through _paths._read_local_paths which fails to find
    <agent>/local-paths.conf and falls back to PROJECT_ROOT/world — a
    different path than the test-writer's WORLD_DIR when the user has
    configured an external world path.
    """
    env = os.environ.copy()
    env["MIND_AGENT"] = agent_name
    env["MIND_WORLD"] = str(WORLD_DIR)
    cmd = [
        sys.executable,
        str(TEAM_STATE_PY),
        "in-flight",
        "--agent", agent_name,
        "--goal-id", f"g-race-{agent_name}",
        "--title", f"race-{agent_name}",
        "--phase", "execute",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def main() -> int:
    # Back up live team-state.yaml (if it exists).
    if TEAM_STATE_PATH.exists():
        BACKUP_PATH.write_bytes(TEAM_STATE_PATH.read_bytes())
        print(f"backed up live team-state.yaml to {BACKUP_PATH.name}")

    # Seed a minimal team-state.yaml so the racers all target the same file
    # starting from the same empty state.
    empty = {
        "strategic_focus": {
            "primary": None,
            "rationale": None,
            "set_by": None,
            "set_at": None,
            "acknowledged_by": [],
        },
        "active_blockers": [],
        "recent_completions": [],
        "agent_status": {},
        "critical_blockers": [],
    }
    try:
        with open(TEAM_STATE_PATH, "w", encoding="utf-8") as f:
            yaml.dump(empty, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)

        # Spawn N racers concurrently. ThreadPoolExecutor with subprocess.run
        # gives us real parallelism for file-locking purposes (each subprocess
        # is a separate OS process).
        agents = [f"test-race-{i}" for i in range(N_RACERS)]
        start = time.time()
        with ThreadPoolExecutor(max_workers=N_RACERS) as ex:
            results = list(ex.map(run_in_flight, agents))
        elapsed = time.time() - start
        print(f"N={N_RACERS} racers completed in {elapsed:.2f}s")

        # Print each subprocess stdout+stderr for visibility.
        for a, r in zip(agents, results):
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            print(f"  {a}: rc={r.returncode} stdout={out!r} stderr={err!r}")

        # Check each subprocess exit code.
        failed = [(a, r) for a, r in zip(agents, results) if r.returncode != 0]
        if failed:
            print(f"TEST FAIL: {len(failed)} subprocess(es) errored:", file=sys.stderr)
            for a, r in failed:
                print(f"  {a}: rc={r.returncode} stderr={r.stderr!r}", file=sys.stderr)
            return 1

        # Read final state and verify all rows survived.
        with open(TEAM_STATE_PATH, "r", encoding="utf-8") as f:
            final = yaml.safe_load(f) or {}
        agent_status = final.get("agent_status") or {}

        missing = []
        for a in agents:
            entry = agent_status.get(a)
            if not entry or "in_flight" not in entry:
                missing.append(a)
                continue
            ifl = entry["in_flight"]
            if ifl.get("goal_id") != f"g-race-{a}":
                missing.append(f"{a}(wrong goal_id: {ifl.get('goal_id')})")

        if missing:
            print(f"TEST FAIL: {len(missing)}/{N_RACERS} row(s) missing or corrupt:", file=sys.stderr)
            for m in missing:
                print(f"  {m}", file=sys.stderr)
            print(f"  agent_status keys: {list(agent_status.keys())}", file=sys.stderr)
            return 1

        print(f"TEST PASS: all {N_RACERS} racers' rows survived")
        return 0
    finally:
        # Restore.
        if BACKUP_PATH.exists():
            TEAM_STATE_PATH.write_bytes(BACKUP_PATH.read_bytes())
            BACKUP_PATH.unlink()
            print("restored live team-state.yaml")


if __name__ == "__main__":
    sys.exit(main())
