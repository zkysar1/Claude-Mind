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

The test seeds a known state into an ISOLATED TMP WORLD, runs the script
against it, and asserts. It never reads or writes the live shared world.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
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

# --- Isolated tmp world — NEVER the live store (guard-708, ) -------
# This test SEEDS and DELETES team-state.yaml. Pointed at the LIVE world (as it
# was until 2026-08-19) it replaced the fleet's shared cross-agent store with a
# two-key stub and leaned on a backup/restore dance, which fails two ways:
#   1. a crash, kill or suite timeout between seed and restore leaves the stub
#      in place — MEASURED, an orphaned `team-state.yaml.rb671-test-backup.79624`
#      was found on DESKTOP-O91DLK2, so that path has already been taken; and
#      `strategic_focus.current_priority` read the literal "test" from the seed
#      below, live, which is the boost directive every product lane depends on;
#   2. even on the happy path the restore CLOBBERS any partner write landing in
#      the window (guard-708 names this as data loss, not just flakiness).
# MIND_WORLD is the highest-priority world override in _paths.sh (L298) and
# _paths.py (L340); team-state.py resolves WORLD_DIR through it at import, so
# redirecting it isolates the subprocess writer as well as this process.
# MIND_META is deliberately NOT redirected — guard-708 says verify rather than
# cargo-cult, and the whole chain (session-manifest-clear.sh -> session_snapshot.py
# -> team-state.py) reads no meta. RT_DIR is likewise not set: guard-1547 applies
# only to tests that can spawn a daemon, and that same chain has ZERO rt_call /
# _runtime.sh reach (positive control: board-post.sh has 3).
_TMP_WORLD = tempfile.mkdtemp(prefix="rb671-world-")
atexit.register(shutil.rmtree, _TMP_WORLD, True)

WORLD_DIR = Path(_TMP_WORLD)
TEAM_STATE_PATH = WORLD_DIR / "team-state.yaml"
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
    # guard-955: pin the backend for the child. Without it, on an own-cloud box
    # the tmp-world write is pushed to the PRODUCTION S3 key — which would make
    # the tmp redirect above look like isolation while still reaching the shared
    # store. The sibling isolated-world test (test_team_state_clear_body_row.py
    # `_cli`) pins it for the same reason.
    env["STORAGE_BACKEND"] = "local"
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
    """Return TEST_AGENT.in_flight from the COMPOSED team state, or None.
    g-328-27 sharding: clear-in-flight seeds+writes the agent's row file, so
    the composed view (row wins newest-wins over the core residual) is the
    correct assertion surface."""
    data = {}
    if TEAM_STATE_PATH.exists():
        data = yaml.safe_load(TEAM_STATE_PATH.read_text(encoding="utf-8")) or {}
    try:
        from _team_state import compose_state
        data = compose_state(data, WORLD_DIR)
    except Exception:
        pass
    entry = (data.get("agent_status") or {}).get(TEST_AGENT) or {}
    return entry.get("in_flight")


def setup_test_agent_dir() -> Path:
    """Create minimal <agent>/session/ + local-paths.conf so MIND_AGENT resolves.
    Returns the agent dir path so the caller can clean it up.

    Uses the Phase 2.5.D agents/ parent layout via _paths.agent_dir() — the
    original PROJECT_ROOT/<name> join broke silently after the relocation
    (session_snapshot.py exited 2 for the unresolvable agent, failing the
    whole manifest-clear), unnoticed because this test is main()-style and
    never pytest-collected."""
    from _paths import agent_dir as resolve_agent_dir
    agent_dir = resolve_agent_dir(TEST_AGENT)
    (agent_dir / "session").mkdir(parents=True, exist_ok=True)
    conf = agent_dir / "local-paths.conf"
    if not conf.exists():
        conf.write_text(f"WORLD_PATH={WORLD_DIR}\n", encoding="utf-8")
    return agent_dir


def teardown_test_agent_dir(agent_dir: Path) -> None:
    """Remove the test agent directory tree. Fail loud — leaving test-rb671/
    behind silently pollutes later runs."""
    if agent_dir.exists():
        shutil.rmtree(agent_dir)


def main() -> int:
    failures: list[str] = []

    # No backup/restore: the world is a throwaway tmp dir (see the module
    # header). Deleting that dance is the point of the fix, not a shortcut —
    # a restore step is exactly what could clobber a partner's write.
    print(f"isolated tmp world: {WORLD_DIR}")

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
        # The agent dir is the one artifact still OUTSIDE the tmp world:
        # agent_dir() resolves under the repo's agents/ parent and is not
        # governed by MIND_WORLD, so it still needs explicit teardown.
        teardown_test_agent_dir(agent_dir)

        # The seeded team-state.yaml and the  row file both live inside
        # the tmp world, which atexit removes wholesale — no per-file cleanup,
        # and nothing to restore, because nothing shared was ever touched.

    if failures:
        print(f"\nFAIL ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS — rb-671 regression test (2/2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
