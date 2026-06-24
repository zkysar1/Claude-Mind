"""Regression test for  — multi-root AGENT_WRITE_PATH in the L1 gate.

The L1 path-resolution hook (`path-resolution-hook.py`) historically supported a
single `AGENT_WRITE_PATH` root. g-321-05 (principal directive 2026-06-07) added
`;`-separated multi-root support so an agent can write to more than one product
workspace (e.g. the Ayoai repo AND the ZDS / Lodestar-Web-App value-spine
workspace) while still denying arbitrary paths.

Each test builds a hermetic temp PROJECT_ROOT with a synthetic agent conf whose
`AGENT_WRITE_PATH` names write root(s) that live OUTSIDE PROJECT_ROOT (so an
allow is attributable to `AGENT_WRITE_PATH`, never to the always-allowed
PROJECT_ROOT root), then invokes the hook as a subprocess and asserts the
permission decision.

Pattern mirrors `test_path_resolution_virtual_prefix_cruft.py` (subprocess
invocation, parse `hookSpecificOutput.permissionDecision`).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
HOOK_PY = CORE_SCRIPTS / "path-resolution-hook.py"


def _invoke(project_root: Path, agent: str, file_path: str) -> str:
    """Run the hook with a synthetic Write payload, return the decision string
    ('approve' on empty stdout, else the parsed permissionDecision)."""
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": file_path},
            "session_id": "test-session",
        }
    )
    env = os.environ.copy()
    env["PROJECT_ROOT"] = project_root.as_posix()
    env["MIND_AGENT"] = agent
    res = subprocess.run(
        [sys.executable, str(HOOK_PY)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project_root),
    )
    if not res.stdout.strip():
        return "approve"
    try:
        return (
            json.loads(res.stdout)
            .get("hookSpecificOutput", {})
            .get("permissionDecision", "approve")
        )
    except json.JSONDecodeError:
        return "approve"


def _make_project(tmp_path: Path, awp_value: str) -> Path:
    """Create PROJECT_ROOT=<tmp>/project with agent 'ta' whose local-paths.conf
    sets AGENT_WRITE_PATH=<awp_value>. Returns the PROJECT_ROOT path."""
    pr = tmp_path / "project"
    agent_dir = pr / "agents" / "ta"
    agent_dir.mkdir(parents=True)
    (agent_dir / "local-paths.conf").write_text(
        f"AGENT_WRITE_PATH={awp_value}\n", encoding="utf-8"
    )
    return pr


def test_multiroot_allows_both_roots(tmp_path):
    # Roots live OUTSIDE PROJECT_ROOT so the allow can only come from AGENT_WRITE_PATH.
    root_a = tmp_path / "RepoA"
    root_b = tmp_path / "RepoB"
    root_a.mkdir()
    root_b.mkdir()
    pr = _make_project(tmp_path, f'"{root_a.as_posix()};{root_b.as_posix()}"')

    assert _invoke(pr, "ta", (root_a / "sub" / "x.ts").as_posix()) == "approve"
    assert _invoke(pr, "ta", (root_b / "deep" / "nest" / "y.py").as_posix()) == "approve"


def test_multiroot_denies_outside_all_roots(tmp_path):
    root_a = tmp_path / "RepoA"
    root_b = tmp_path / "RepoB"
    outside = tmp_path / "Outside"
    root_a.mkdir()
    root_b.mkdir()
    outside.mkdir()
    pr = _make_project(tmp_path, f'"{root_a.as_posix()};{root_b.as_posix()}"')

    assert _invoke(pr, "ta", (outside / "evil.txt").as_posix()) == "deny"


def test_single_root_backward_compat(tmp_path):
    # No ';' and no quotes — the pre- form must still allow.
    root_a = tmp_path / "RepoA"
    root_a.mkdir()
    pr = _make_project(tmp_path, root_a.as_posix())

    assert _invoke(pr, "ta", (root_a / "x.txt").as_posix()) == "approve"
    # An unconfigured sibling is still denied with a single root.
    other = tmp_path / "Other"
    other.mkdir()
    assert _invoke(pr, "ta", (other / "x.txt").as_posix()) == "deny"


def test_quoted_single_root_strips_quotes(tmp_path):
    # A quoted single value must parse identically to the unquoted form.
    root_a = tmp_path / "RepoA"
    root_a.mkdir()
    pr = _make_project(tmp_path, f'"{root_a.as_posix()}"')

    assert _invoke(pr, "ta", (root_a / "x.txt").as_posix()) == "approve"
