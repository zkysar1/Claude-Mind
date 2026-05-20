"""test_recovery_clears_team_state_in_flight.py — rb-671 regression test.

Verifies that session-manifest-clear.sh clears the cross-agent in_flight
snapshot in world/team-state.yaml as part of the recovery sweep. Before
rb-671, manifest-clear cleaned agent-local files but left
agent_status.<agent>.in_flight orphaned — partners would see the recovered
agent as indefinitely mid-execution on its abandoned goal.

Two assertions:
  1. With team-state populated: in_flight is null after manifest-clear.
  2. Without team-state.yaml at all: manifest-clear still exits 0
     (fail-open invariant — recovery must complete even if cross-agent
     cleanup errors).

The test backs up the live team-state.yaml, seeds a known state, runs the
script, asserts, then restores the backup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

from _paths import WORLD_DIR  # type: ignore

TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"
BACKUP_PATH = TEAM_STATE_PATH.with_suffix(f".yaml.rb671-test-backup.{os.getpid()}")
MANIFEST_CLEAR_SH = CORE_SCRIPTS / "session-manifest-clear.sh"
TEST_AGENT = "test-rb671"


def seed_team_state_with_in_flight() -> None:
    """Write a team-state.yaml with TEST_AGENT.in_flight populated."""
    seed = {
        "strategic_focus": {"current_priority": "test"},
        "agent_status": {
            TEST_AGENT: {
                "in_flight": {
                    "goal_id": "g-test-001",
                    "title": "test goal for rb-671",
                    "claimed_at": "2026-05-01T14:07:10",
                    "phase": "4",
                },
                "last_active": "2026-05-01T14:07:10",
            }
        },
    }
    TEAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEAM_STATE_PATH.write_text(yaml.safe_dump(seed), encoding="utf-8")


def resolve_bash() -> str:
    """Pick a bash binary that inherits the Python parent's environment.

    On Windows the bare `bash` resolves to WSL bash, which sandboxes its
    environment from the Windows parent — custom env vars (MIND_AGENT,
    MIND_WORLD) silently disappear unless listed in WSLENV. The framework's
    own scripts run via Git Bash (the project's documented shell), so the
    test mirrors that. On Linux/macOS the bare `bash` is correct.
    """
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
    return "bash"


def run_manifest_clear() -> subprocess.CompletedProcess:
    """Invoke session-manifest-clear.sh against the test agent."""
    env = os.environ.copy()
    env["MIND_AGENT"] = TEST_AGENT
    env["MIND_WORLD"] = str(WORLD_DIR)
    # Forward-slash relative path; Git Bash on Windows mangles drive-letter
    # paths (Errno 2 / "No such file or directory") when the colon survives.
    return subprocess.run(
        [resolve_bash(), "core/scripts/session-manifest-clear.sh"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        cwd=str(PROJECT_ROOT),
    )


def read_in_flight() -> object:
    """Return TEST_AGENT.in_flight from current team-state.yaml, or None."""
    if not TEAM_STATE_PATH.exists():
        return None
    data = yaml.safe_load(TEAM_STATE_PATH.read_text(encoding="utf-8")) or {}
    return (data.get("agent_status") or {}).get(TEST_AGENT, {}).get("in_flight")


def setup_test_agent_dir() -> Path:
    """Create minimal <agent>/session/ + local-paths.conf so MIND_AGENT resolves.
    Returns the agent dir path so the caller can clean it up."""
    agent_dir = PROJECT_ROOT / TEST_AGENT
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    conf = agent_dir / "local-paths.conf"
    if not conf.exists():
        conf.write_text(f"WORLD_PATH={WORLD_DIR}\n", encoding="utf-8")
    return agent_dir


def teardown_test_agent_dir(agent_dir: Path) -> None:
    """Remove the test agent directory tree. Fail loud — leaving test-rb671/
    behind silently pollutes later runs."""
    import shutil

    if agent_dir.exists():
        shutil.rmtree(agent_dir)


def main() -> int:
    failures: list[str] = []

    # Back up live team-state.yaml.
    backed_up = False
    if TEAM_STATE_PATH.exists():
        BACKUP_PATH.write_bytes(TEAM_STATE_PATH.read_bytes())
        backed_up = True
        print(f"backed up live team-state.yaml to {BACKUP_PATH.name}")

    agent_dir = setup_test_agent_dir()

    try:
        # === Test 1: in_flight populated → cleared after manifest-clear ===
        print("\n[test 1] populated team-state.yaml → in_flight cleared")
        seed_team_state_with_in_flight()
        before = read_in_flight()
        assert before is not None, "seed precondition: in_flight should be set"
        print(f"  before: in_flight = {before}")

        result = run_manifest_clear()
        print(f"  exit: {result.returncode}")
        if result.stdout:
            print(f"  stdout:\n{result.stdout}")
        if result.stderr:
            print(f"  stderr:\n{result.stderr}")

        if result.returncode != 0:
            failures.append(
                f"test 1: manifest-clear exit={result.returncode} (expected 0)"
            )

        after = read_in_flight()
        print(f"  after:  in_flight = {after}")
        if after is not None:
            failures.append(
                f"test 1: in_flight should be cleared, got {after!r}"
            )

        # === Test 2: missing team-state.yaml → manifest-clear still exits 0 ===
        print("\n[test 2] missing team-state.yaml → fail-open recovery still completes")
        if TEAM_STATE_PATH.exists():
            TEAM_STATE_PATH.unlink()

        result = run_manifest_clear()
        print(f"  exit: {result.returncode}")
        if result.stderr:
            print(f"  stderr:\n{result.stderr}")

        if result.returncode != 0:
            failures.append(
                f"test 2: manifest-clear exit={result.returncode} with missing team-state (expected 0; fail-open)"
            )

    finally:
        # Tear down test agent dir.
        teardown_test_agent_dir(agent_dir)

        # Restore live team-state.yaml.
        if backed_up:
            TEAM_STATE_PATH.write_bytes(BACKUP_PATH.read_bytes())
            BACKUP_PATH.unlink()
            print(f"\nrestored live team-state.yaml from backup")
        else:
            # No live file existed before the test — make sure we don't leave
            # the test seed behind.
            if TEAM_STATE_PATH.exists():
                TEAM_STATE_PATH.unlink()

    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS — rb-671 regression test (2/2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
