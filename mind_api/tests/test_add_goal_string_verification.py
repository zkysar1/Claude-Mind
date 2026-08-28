"""A goal whose ``verification`` is a plain string is refused with a clear validation
error — never an internal crash.

Measured 2026-08-28 (coach, zc-03): an assistant session filed
``"verification": "A scan of both files confirms zero collisions"`` and the daemon
answered ``{"error": "internal_error", "detail": "AttributeError: 'str' object has no
attribute 'get'"}``. The model read that as "the daemon is broken" and hand-wrote the
goal into the JSONL store — the exact bypass the store discipline forbids. A shape
error must name the shape.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "core" / "scripts" / "aspirations-add-goal.sh"


def _run(args, *, stdin: str, project_root: Path, agent: str = "alpha"):
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    proc = subprocess.run(
        [shutil.which("bash") or "bash", WRAPPER.as_posix(), *args],
        env=env, input=stdin, capture_output=True, text=True, check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_string_verification_is_a_validation_error_not_a_crash(running_daemon):
    project_root, _ = running_daemon
    goal = {
        "title": "Investigate: string verification",
        "status": "pending",
        "origin_signal": "user_directive",
        "description": "x" * 100,
        "verification": "A scan of both aspiration files confirms zero collisions.",
    }
    rc, out, err = _run(["asp-001"], stdin=json.dumps(goal), project_root=project_root)
    assert rc != 0
    combined = out + err
    assert "internal_error" not in combined, combined
    assert "validation_failed" in combined, combined
    # The error names the shape the model must send.
    assert "verification" in combined and "outcomes" in combined, combined
