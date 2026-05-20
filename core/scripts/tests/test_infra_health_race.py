"""test_infra_health_race.py — concurrency smoke test for .

Verifies that concurrent writers targeting DIFFERENT components of
world/infra-health.yaml all survive. Before locked_modify_yaml (g-248-19),
cmd_check / cmd_check_all / cmd_record did unlocked load_health → mutate →
save_health: two agents probing different components could both read the
same baseline and the second writer's component update would silently
clobber the first's. This test catches regressions of that pattern.

Uses `cmd_record` (no external probe, so no 30s subprocess timeouts) to
keep the test fast and deterministic. Locking path is identical to
`cmd_check` / `cmd_check_all` — they all route through the same
locked_modify_yaml in _fileops.py.

The test backs up the live infra-health.yaml, seeds an empty one with N
test components declared, runs N concurrent cmd_record subprocesses each
recording success against a different component, asserts every test
component shows last_success set, then restores the backup.

Pass: all N components' updates survive.
Fail: any component missing a last_success (clobbered).

Same authoring pattern as test_team_state_race.py (g-240-52).
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

HEALTH_PATH = WORLD_DIR / "infra-health.yaml"
BACKUP_PATH = HEALTH_PATH.with_suffix(f".yaml.race-test-backup.{os.getpid()}")
N_RACERS = 8
INFRA_PY = CORE_SCRIPTS / "infra-health.py"


def run_record(component: str) -> subprocess.CompletedProcess:
    """Invoke `infra-health.py record <component> success <summary>` for one racer.

    Passes MIND_WORLD explicitly so the subprocess resolves the same
    infra-health.yaml as the test.
    """
    env = os.environ.copy()
    env["MIND_WORLD"] = str(WORLD_DIR)
    cmd = [
        sys.executable,
        str(INFRA_PY),
        "record",
        component,
        "success",
        f"race-test-{component}",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)


def main() -> int:
    # Back up live infra-health.yaml (if it exists).
    if HEALTH_PATH.exists():
        BACKUP_PATH.write_bytes(HEALTH_PATH.read_bytes())
        print(f"backed up live infra-health.yaml to {BACKUP_PATH.name}")

    # Seed minimal infra-health.yaml with N test components declared so
    # record_result doesn't need to auto-create each under concurrent pressure.
    # (auto-creation is also locked now, but we want to isolate the write-race
    # from the create-race.)
    components_to_seed = {
        f"test-race-{i}": {
            "last_success": None,
            "last_failure": None,
            "last_failure_reason": None,
            "consecutive_failures": 0,
            "session_last_checked": None,
        }
        for i in range(N_RACERS)
    }
    seeded = {"components": components_to_seed}

    try:
        with open(HEALTH_PATH, "w", encoding="utf-8") as f:
            yaml.dump(seeded, f, default_flow_style=False, allow_unicode=True,
                      sort_keys=False)

        # Spawn N racers concurrently. ThreadPoolExecutor + subprocess.run
        # gives real parallelism (each subprocess is a separate OS process,
        # each racing for the infra-health.yaml lock).
        components = [f"test-race-{i}" for i in range(N_RACERS)]
        start = time.time()
        with ThreadPoolExecutor(max_workers=N_RACERS) as ex:
            results = list(ex.map(run_record, components))
        elapsed = time.time() - start
        print(f"N={N_RACERS} racers completed in {elapsed:.2f}s")

        for c, r in zip(components, results):
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            print(f"  {c}: rc={r.returncode} stdout={out!r} stderr={err[:80]!r}")

        failed = [(c, r) for c, r in zip(components, results) if r.returncode != 0]
        if failed:
            print(f"TEST FAIL: {len(failed)} subprocess(es) errored:", file=sys.stderr)
            for c, r in failed:
                print(f"  {c}: rc={r.returncode} stderr={r.stderr!r}", file=sys.stderr)
            return 1

        # Read final state and verify all components got their last_success bump.
        # Pre-locked_modify_yaml, the unlocked read-then-write would lose some of
        # these under concurrent pressure.
        with open(HEALTH_PATH, "r", encoding="utf-8") as f:
            final = yaml.safe_load(f) or {}
        final_components = final.get("components") or {}

        missing = []
        for c in components:
            entry = final_components.get(c) or {}
            last_success = entry.get("last_success")
            if not last_success:
                missing.append(c)
                continue

        if missing:
            print(f"TEST FAIL: {len(missing)}/{N_RACERS} component(s) missing last_success:", file=sys.stderr)
            for m in missing:
                print(f"  {m}: entry={final_components.get(m)}", file=sys.stderr)
            return 1

        print(f"TEST PASS: all {N_RACERS} racers' component updates survived")
        return 0
    finally:
        # Restore.
        if BACKUP_PATH.exists():
            HEALTH_PATH.write_bytes(BACKUP_PATH.read_bytes())
            BACKUP_PATH.unlink()
            print("restored live infra-health.yaml")


if __name__ == "__main__":
    sys.exit(main())
