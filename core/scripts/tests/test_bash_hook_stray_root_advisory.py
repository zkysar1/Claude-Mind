#!/usr/bin/env python3
"""Stray repo-root world|meta advisory (2026-08-28 charter incident).

A file created by a script body (py -3 patch.py building paths internally)
bypasses BOTH L1 write-time hooks by construction, so detection — not
prevention — is the closure: the Bash hook fires before every write-shaped
command and one isdir surfaces the stray within a call or two of creation.

Drives the hook end-to-end via subprocess + stdin payload with PROJECT_ROOT
and MIND_AGENT set (the production invocation shape — pattern (b) from the
hook-authoring-pitfalls node; and the payload is BUILT WITH json.dumps, never
hand-quoted through a shell: the incident's own diagnosis was delayed an hour
by probes whose backslashes a shell layer collapsed into invalid JSON that
the hook silently fail-open-approved).

Pins:
  * stray world/ present + conf pointing elsewhere  -> advisory (allow +
    4-channel message naming the stray and the configured root)
  * stray ABSENT                                    -> silent approve
  * conf pointing WORLD_PATH AT the repo-root dir   -> silent approve (a
    legitimate local layout is not a stray)
  * the advisory is allow, never deny               -> permissionDecision
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
BASH_HOOK = SCRIPTS / "bash-path-resolution-hook.py"

AGENT = "strayprobe"
# Write-shaped command so the hook's fast filter does not approve early.
CMD = "mkdir -p /tmp/x && cp a b"


def run_hook(project_root: str, command: str = CMD) -> dict | None:
    payload = json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    env = dict(os.environ)
    env["PROJECT_ROOT"] = project_root
    env["MIND_AGENT"] = AGENT
    proc = subprocess.run(
        [sys.executable, str(BASH_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def make_root(world_conf: str | None, with_stray_world: bool) -> str:
    root = tempfile.mkdtemp(prefix="strayroot-")
    agent_dir = Path(root) / "agents" / AGENT
    agent_dir.mkdir(parents=True)
    external = Path(root) / "external-world"
    external.mkdir()
    conf_world = world_conf if world_conf is not None else str(external)
    (agent_dir / "local-paths.conf").write_text(
        f"WORLD_PATH={conf_world}\nMETA_PATH={root}/external-meta\n",
        encoding="utf-8",
    )
    if with_stray_world:
        (Path(root) / "world" / "conventions").mkdir(parents=True)
    return root


class StrayRootAdvisory(unittest.TestCase):
    def test_stray_world_fires_the_advisory(self):
        root = make_root(world_conf=None, with_stray_world=True)
        res = run_hook(root)
        self.assertIsNotNone(res, "expected an advisory payload, got silence")
        hso = res.get("hookSpecificOutput", {})
        self.assertEqual(hso.get("permissionDecision"), "allow")
        reason = str(hso.get("permissionDecisionReason", ""))
        self.assertIn("stray-root-advisory", reason)
        self.assertIn("world", reason)
        # The 4-channel delivered shape () — all fields present.
        self.assertTrue(hso.get("additionalContext"))
        self.assertTrue(res.get("systemMessage"))

    def test_no_stray_stays_silent(self):
        root = make_root(world_conf=None, with_stray_world=False)
        self.assertIsNone(run_hook(root))

    def test_legitimate_repo_root_world_layout_is_not_a_stray(self):
        root = make_root(world_conf="PLACEHOLDER", with_stray_world=True)
        # Point WORLD_PATH at the repo-root world dir itself.
        conf = Path(root) / "agents" / AGENT / "local-paths.conf"
        conf.write_text(
            f"WORLD_PATH={root}/world\nMETA_PATH={root}/external-meta\n",
            encoding="utf-8",
        )
        self.assertIsNone(run_hook(root))

    def test_advisory_never_denies(self):
        root = make_root(world_conf=None, with_stray_world=True)
        res = run_hook(root)
        self.assertIsNotNone(res)
        self.assertNotEqual(
            res.get("hookSpecificOutput", {}).get("permissionDecision"), "deny"
        )


if __name__ == "__main__":
    unittest.main()
