#!/usr/bin/env python3
"""Cross-agent write advisory ( outcome 2).

MEASURED 2026-09-23 on zc-01: a worker Body bound to alpha lost its identity
across a compaction and wrote its evidence under
agents/charlie/sessions/<own SID>/scratch/, then cited that file as closure
evidence. Both L1 hooks approved the write in silence, because every agent-dir
check keys on the BOUND agent's dir. Now a write under another agent's dir gets
an advisory naming the binding: allow, never deny.

Drives both hooks end-to-end: subprocess, a json.dumps payload, PROJECT_ROOT in
the env (the production shape). One case resolves the agent from the SID's
binding.yaml with no MIND_AGENT in the env, which is a zakcode Body's hook env.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from _path_roots import cross_agent_owner  # noqa: E402

WRITE_HOOK = SCRIPTS / "path-resolution-hook.py"
BASH_HOOK = SCRIPTS / "bash-path-resolution-hook.py"
SID = "sid-g37504"


def make_root() -> Path:
    """A project root with a bound alpha session and a second agent, charlie."""
    root = Path(tempfile.mkdtemp(prefix="xagent-"))
    for name in ("alpha", "charlie"):
        agent_dir = root / "agents" / name
        (agent_dir / "sessions" / SID / "scratch").mkdir(parents=True)
        (agent_dir / "local-paths.conf").write_text(
            f"WORLD_PATH={root}/ext-world\nMETA_PATH={root}/ext-meta\n",
            encoding="utf-8")
    (root / "ext-world").mkdir()
    (root / "ext-meta").mkdir()
    (root / "agents" / "alpha" / "sessions" / SID / "binding.yaml").write_text(
        f"session_id: {SID}\nagent: alpha\nmode: autonomous\n", encoding="utf-8")
    return root


def run(hook: Path, payload: dict, root: Path, agent: str | None = "alpha") -> dict | None:
    env = dict(os.environ)
    env["PROJECT_ROOT"] = str(root)
    env.pop("MIND_AGENT", None)
    if agent:
        env["MIND_AGENT"] = agent
    proc = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=60)
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def write(root: Path, rel: str, tool: str = "Write", sid: str = SID) -> dict:
    return {"tool_name": tool, "session_id": sid,
            "tool_input": {"file_path": str(root / rel), "content": "x"}}


def bash(command: str, sid: str = SID) -> dict:
    return {"tool_name": "Bash", "session_id": sid, "tool_input": {"command": command}}


CHARLIE = f"agents/charlie/sessions/{SID}/scratch/rca.md"
OWN = f"agents/alpha/sessions/{SID}/scratch/rca.md"


class OwnerPredicate(unittest.TestCase):
    def test_owner_is_the_other_agent(self):
        bound = "/r/agents/alpha"
        self.assertEqual(cross_agent_owner("/r/agents/charlie/sessions/s/f.md", bound), "charlie")
        # A sibling whose name starts with the bound name is still another agent.
        self.assertEqual(cross_agent_owner("/r/agents/alpha-next/f.md", bound), "alpha-next")

    def test_no_owner_outside_another_agents_dir(self):
        bound = "/r/agents/alpha"
        for target in ("/r/agents/alpha", "/r/agents/alpha/temp/f.md",
                       "/r/agents/notes.md", "/r/agents", "/r/core/scripts/x.py", ""):
            self.assertIsNone(cross_agent_owner(target, bound), target)
        self.assertIsNone(cross_agent_owner("/r/agents/charlie/f.md", ""))


class WriteHook(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def assert_advised(self, res, surface):
        self.assertIsNotNone(res, "expected an advisory payload, got silence")
        out = res["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        text = out["additionalContext"]
        self.assertIn(f"bound to agent 'alpha' (SID {SID})", text)
        self.assertIn("another agent's directory, 'charlie'", text)
        self.assertIn(surface, text)
        return text

    def test_write_into_another_agents_dir_is_advised(self):
        text = self.assert_advised(run(WRITE_HOOK, write(self.root, CHARLIE), self.root), "Write")
        self.assertIn(str(self.root / CHARLIE), text)

    def test_edit_is_advised_too(self):
        self.assert_advised(run(WRITE_HOOK, write(self.root, CHARLIE, "Edit"), self.root), "Edit")

    def test_agent_resolved_from_the_sid_binding(self):
        # No MIND_AGENT: the hook must resolve alpha from binding.yaml.
        self.assert_advised(run(WRITE_HOOK, write(self.root, CHARLIE), self.root, agent=None), "Write")

    def test_own_dir_write_is_silent(self):
        self.assertIsNone(run(WRITE_HOOK, write(self.root, OWN), self.root))

    def test_unbound_session_is_silent(self):
        payload = write(self.root, CHARLIE, sid="never-bound")
        self.assertIsNone(run(WRITE_HOOK, payload, self.root, agent=None))


class BashHook(unittest.TestCase):
    def setUp(self):
        self.root = make_root()

    def test_redirect_into_another_agents_dir_is_advised(self):
        res = run(BASH_HOOK, bash(f"echo x > {CHARLIE}"), self.root)
        self.assertIsNotNone(res, "expected an advisory payload, got silence")
        out = res["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "allow")
        self.assertIn(f"bound to agent 'alpha' (SID {SID})", out["additionalContext"])
        self.assertIn("this Bash (", out["additionalContext"])

    def test_own_dir_redirect_is_silent(self):
        self.assertIsNone(run(BASH_HOOK, bash(f"echo x > {OWN}"), self.root))

    def test_both_advisories_ride_together_with_a_stray_root(self):
        # The stray-root advisory is collected, not emitted early, so it must
        # not swallow the cross-agent one (or anything after it).
        (self.root / "world").mkdir()
        res = run(BASH_HOOK, bash(f"echo x > {CHARLIE}"), self.root)
        self.assertIsNotNone(res)
        text = res["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[stray-root-advisory]", text)
        self.assertIn("[cross-agent-write]", text)

    def test_a_deny_for_another_target_still_wins(self):
        cmd = f"echo x > {CHARLIE}; echo y > /nonexistent-root-g37504/f.txt"
        res = run(BASH_HOOK, bash(cmd), self.root)
        self.assertIsNotNone(res)
        self.assertEqual(res["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
