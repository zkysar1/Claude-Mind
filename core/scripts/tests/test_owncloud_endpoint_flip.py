"""owncloud-endpoint-flip.sh — the P0 user-identity precondition (2026-09-11).

The flip tool restarts the daemon, and a daemon is spawned as whoever runs the
tool. Its rightful owner is the owner of ``.env.local``. P0 refuses any other
user BEFORE any probe or write, naming the runuser line to re-run.

The positive case (a file owned by someone else) can only be built as root —
the fleet's containers run their suites as root, so it runs there and skips
elsewhere. No privilege-dropping wrapper is stubbed (guard-2707): the real
script runs against a real file with a real foreign owner.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))
from _runtime_bash import BASH  # noqa: E402

TOOL = REPO / "core" / "scripts" / "owncloud-endpoint-flip.sh"


def _fake_root(tmp_path: Path, env_text: str) -> Path:
    root = tmp_path / "repo"
    (root / "core" / "scripts").mkdir(parents=True)
    # the tool only checks this file EXISTS before P0 fires
    (root / "core" / "scripts" / "owncloud-endpoint-probe.py").write_text("# stub\n")
    (root / ".env.local").write_text(env_text)
    os.chmod(root / ".env.local", 0o600)
    return root


def _run(root: Path, *args: str) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k != "STORAGE_S3_ENDPOINT_URL"}
    env["REPO_ROOT"] = str(root)
    r = subprocess.run([BASH, TOOL.as_posix(), *args], cwd=str(root), env=env,
                       capture_output=True, text=True, timeout=60)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    try:
        out = json.loads(last) if last else {}
    except json.JSONDecodeError:
        out = {"_raw": r.stdout}
    out["_err"] = r.stderr[-800:]   # surfaced in every assertion message
    return r.returncode, out


@pytest.mark.skipif(os.geteuid() != 0, reason="building a foreign-owned .env.local needs root")
def test_wrong_user_is_refused_at_p0_before_any_probe(tmp_path):
    root = _fake_root(tmp_path, "STORAGE_BACKEND=own-cloud\n")
    os.chown(root / ".env.local", 65534, 65534)
    for args in (("--to", "http://127.0.0.1:1", "--dry-run"), ("--status",), ("--revert", "--dry-run")):
        rc, out = _run(root, *args)
        assert rc == 1, (args, out)
        assert out.get("ok") is False and out.get("stage") == "P0", (args, out)
        detail = out.get("detail", "")
        assert "65534" in detail and "runuser -u" in detail and "owncloud-endpoint-flip.sh" in detail, detail


def test_same_user_passes_p0_and_reaches_p1(tmp_path):
    # Owned by the caller: P0 is silent and the next refusal is P1 (not own-cloud).
    root = _fake_root(tmp_path, "STORAGE_BACKEND=local\n")
    rc, out = _run(root, "--to", "http://127.0.0.1:1", "--dry-run")
    assert rc == 1, out
    assert out.get("stage") == "P1", out
