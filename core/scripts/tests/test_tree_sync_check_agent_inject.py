"""test_tree_sync_check_agent_inject.py —  regression test.

tree-sync-check.sh is invoked from PostToolUse[Edit|Write|MultiEdit] hooks.
These hooks do NOT receive MIND_AGENT via env (only PreToolUse[Bash] gets
that, via bash-agent-inject). tree-sync-check.sh chains to
uncommitted-edits-record.sh, which exits early if MIND_AGENT is unbound,
leaving <agent>/session/uncommitted-edits.jsonl empty.

This regression test exercises the hoisted agent-resolution branch added in
g-115-873: tree-sync-check.sh reads `.active-agent-<session_id>` and
env-injects MIND_AGENT="<agent>" on the chained call. The fix surfaces in
the appended JSONL entry — without it, the log stays empty.

Test strategy: run tree-sync-check.sh in a subprocess with MIND_AGENT
EXPLICITLY UNSET, passing a hook payload that names a valid SID. Assert the
log file gains one entry referencing the file path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Add core/scripts to path for any helper imports
SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _to_bash_path(p) -> str:
    """Convert a Windows-style path to git-bash POSIX form (mirrors
    test_iteration_commit_concurrent_partner.py — required because raw
    backslashes get stripped when git-bash launched-via-subprocess receives
    them as positional args)."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


class TestTreeSyncCheckAgentInject(unittest.TestCase):
    """Verify tree-sync-check.sh injects MIND_AGENT into uncommitted-edits chain."""

    def setUp(self):
        # Create a temporary fake agent dir under PROJECT_ROOT so the test
        # writes go somewhere we can read + clean up without touching real
        # alpha/bravo/etc. state.
        self.tmp_agent_name = "g115873test"
        # Phase 2.5.D layout: agent dirs live under agents/ parent.
        self.agent_dir = PROJECT_ROOT / "agents" / self.tmp_agent_name
        self.session_dir = self.agent_dir / "session"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.session_dir / "uncommitted-edits.jsonl"
        if self.log_path.exists():
            self.log_path.unlink()
        # Create the local-paths.conf so _paths.sh recognizes the agent dir
        # as a valid agent (mirrors real agent layout).
        (self.agent_dir / "self.md").write_text(
            "---\nname: g115873test\n---\n# Test fixture agent\n", encoding="utf-8"
        )
        # Sandbox WORLD_PATH/META_PATH UNDER the fixture agent dir. The SUT
        # (tree-sync-check.sh) chains to meta-writers (gate-firings via
        # _gate_log.py, changelog, trigger-firings) that resolve META_DIR
        # from this conf. Pointing them at the real PROJECT_ROOT/{world,meta}
        # let every run leak telemetry into the repo root, surviving tearDown
        # (which only rmtree's agent_dir). Sandboxing under agent_dir means
        # tearDown's existing rmtree cleans the telemetry too. (5 —
        # orphan-root cruft; same pollution class as guard-652 l1-pick-log
        # isolation, but via the local-paths.conf mechanism rather than env.)
        self.sandbox_world = self.agent_dir / "world"
        self.sandbox_meta = self.agent_dir / "meta"
        self.sandbox_world.mkdir(parents=True, exist_ok=True)
        self.sandbox_meta.mkdir(parents=True, exist_ok=True)
        (self.agent_dir / "local-paths.conf").write_text(
            f"WORLD_PATH={self.sandbox_world}\n"
            f"META_PATH={self.sandbox_meta}\n",
            encoding="utf-8",
        )
        # Create the .active-agent-<sid> binding file (this is what
        # tree-sync-check.sh reads to resolve the agent).
        self.test_sid = "test-g115873-sid-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.binding_path = PROJECT_ROOT / f".active-agent-{self.test_sid}"
        self.binding_path.write_text(self.tmp_agent_name, encoding="utf-8")

    def tearDown(self):
        # 2026-05-19 (plan v1 step 0.7 — RC-C): the previous tearDown used
        # individual unlink + rmdir(). The SUT (tree-sync-check.sh) chains to
        # scripts that write side-effect files into the fixture session dir
        # (board-activity, goal-claim-released, etc.). rmdir() then fails with
        # ENOTEMPTY and the fixture directory `g115873test/` was leaking back
        # into the repo root every pytest run. shutil.rmtree(ignore_errors=True)
        # handles arbitrary side-effect content cleanly.
        import shutil
        try:
            if self.binding_path.exists():
                self.binding_path.unlink()
        except Exception:
            pass
        shutil.rmtree(self.agent_dir, ignore_errors=True)

    def _run_hook(self, file_path: str) -> tuple[int, str, str]:
        """Run tree-sync-check.sh with the given Edit payload, MIND_AGENT unset.

        Returns (returncode, stdout, stderr).
        """
        payload = json.dumps({
            "session_id": self.test_sid,
            "tool_name": "Edit",
            "tool_input": {"file_path": file_path},
        })
        # Build env WITHOUT MIND_AGENT — this is the PostToolUse hook reality.
        env = {k: v for k, v in os.environ.items() if k != "MIND_AGENT"}
        script_posix = _to_bash_path(CORE_SCRIPTS / "tree-sync-check.sh")
        # Use the resolved GIT_BASH (mirrors test_iteration_commit_*) — passing
        # raw Windows paths to bash via subprocess strips backslashes.
        proc = subprocess.run(
            [GIT_BASH, script_posix],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(PROJECT_ROOT),
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_chain_writes_log_when_agent_resolved_from_sid(self):
        """With MIND_AGENT unset but SID binding present, log gains an entry."""
        # The file_path must be a neutral path (not under any agent dir) for
        # uncommitted-edits-record.sh to record it. Use a path under
        # core/scripts/ — the fixture agent dir is sibling to that.
        target = "core/scripts/tree-sync-check.sh"
        rc, out, err = self._run_hook(target)
        self.assertEqual(rc, 0, f"hook exit non-zero: stderr={err}")
        self.assertTrue(self.log_path.exists(),
                        f"log file not created: stderr={err}")
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1,
                         f"expected 1 log line, got {len(lines)}: {lines}")
        entry = json.loads(lines[0])
        self.assertEqual(entry["file"], target)
        self.assertIn("mtime", entry)
        self.assertIn("edit_ts", entry)

    def test_chain_no_op_when_no_sid_in_payload(self):
        """Without session_id, agent cannot be resolved → record exits → no log."""
        payload = json.dumps({
            "tool_name": "Edit",
            "tool_input": {"file_path": "core/scripts/tree-sync-check.sh"},
            # No session_id field
        })
        env = {k: v for k, v in os.environ.items() if k != "MIND_AGENT"}
        script_posix = _to_bash_path(CORE_SCRIPTS / "tree-sync-check.sh")
        proc = subprocess.run(
            [GIT_BASH, script_posix],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(PROJECT_ROOT),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0,
                         f"hook exit non-zero: stderr={proc.stderr}")
        # Log file should NOT exist (uncommitted-edits-record.sh exited early)
        self.assertFalse(self.log_path.exists(),
                         "log file unexpectedly created without sid binding")

    def test_chain_no_op_for_agent_dir_path(self):
        """Edits inside an agent dir skip the log (neutral-path filter)."""
        target = f"{self.tmp_agent_name}/aspirations.jsonl"
        rc, out, err = self._run_hook(target)
        self.assertEqual(rc, 0, f"hook exit non-zero: stderr={err}")
        # Log file should NOT exist — record script skips agent-dir paths
        self.assertFalse(self.log_path.exists(),
                         "log file unexpectedly created for agent-dir path")


if __name__ == "__main__":
    unittest.main()
