"""test_daemon_orphan_sweep_cross_repo.py —  regression test.

Cross-repo-safety of daemon-orphan-sweep.sh: --clean must NOT kill a sibling
Mind deployment's live daemon. Before g-328-08 the keep-set was ONLY the local
repo's published pair while the process scan is system-wide (Win32_Process /
pgrep expose no cwd/env discriminator), so --clean run from one repo on a
multi-deployment machine killed sibling repos' daemons — and
test_daemon_orphan_prevention.py had to SKIP on multi-repo to avoid exactly
that collateral.

These tests exercise the keep-set DISCOVERY in isolation via the
`--print-keepset` debug mode (no process scan, no kill), pointing the local
state at RUNTIME_DIR and the sibling-discovery root at ORPHAN_SWEEP_DEPLOY_PARENT
— both tmpdirs. Fully hermetic: no real daemons are spawned, so this file is NOT
daemon_integration-marked and runs in the daemon-safe suite
(`pytest -m "not daemon_integration"`).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SWEEP_SH = CORE_SCRIPTS / "daemon-orphan-sweep.sh"


def _fwd(p) -> str:
    """Forward-slash form — bash globbing chokes on Windows backslashes."""
    return str(p).replace("\\", "/")


def _make_repo(root: Path, name: str, child: int, parent=None,
               layout: str = "mind_api/state") -> Path:
    state = root / name / layout
    state.mkdir(parents=True, exist_ok=True)
    (state / "daemon.pid").write_text(str(child), encoding="utf-8")
    if parent is not None:
        (state / "daemon.parent.pid").write_text(str(parent), encoding="utf-8")
    return root / name


def _print_keepset(local_state: Path, deploy_parent: Path, *extra_args) -> dict:
    env = dict(os.environ)
    env["RUNTIME_DIR"] = _fwd(local_state)
    env["ORPHAN_SWEEP_DEPLOY_PARENT"] = _fwd(deploy_parent)
    cmd = [BASH, _fwd(SWEEP_SH), "--print-keepset", *extra_args]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        env=env, cwd=str(PROJECT_ROOT),
    )
    out = {"_rc": proc.returncode, "_stderr": proc.stderr, "_stdout": proc.stdout}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _pids(keepset_value: str) -> set:
    return {x for x in (keepset_value or "").split(",") if x}


def test_sibling_repo_daemon_is_protected(tmp_path):
    """A sibling deployment's published pair MUST be in the keep-set so
    --clean never kills it. The core g-328-08 acceptance (a)."""
    local = _make_repo(tmp_path, "repo_local", 1111, 2222)
    _make_repo(tmp_path, "repo_sibling", 3333, 4444)
    res = _print_keepset(local / "mind_api" / "state", tmp_path)
    assert res["_rc"] == 0, res
    pids = _pids(res.get("KEEPSET_PIDS"))
    assert {"1111", "2222"} <= pids, f"local pair missing: {pids}"
    assert {"3333", "4444"} <= pids, (
        f"sibling daemon NOT protected — --clean would kill it: {pids}"
    )


def test_mind_data_layout_discovered(tmp_path):
    """The .mind-data/mind_api/state/ layout variant is also discovered."""
    local = _make_repo(tmp_path, "repo_local", 1111, 2222)
    _make_repo(tmp_path, "repo_alt", 5555, 6666,
               layout=".mind-data/mind_api/state")
    res = _print_keepset(local / "mind_api" / "state", tmp_path)
    pids = _pids(res.get("KEEPSET_PIDS"))
    assert {"5555", "6666"} <= pids, f".mind-data layout not discovered: {pids}"


def test_keep_repo_flag_adds_out_of_tree_repo(tmp_path):
    """--keep-repo protects a deployment outside the auto-discovery parent."""
    local = _make_repo(tmp_path, "repo_local", 1111, 2222)
    elsewhere = _make_repo(tmp_path / "out", "repo_x", 7777, 8888)
    res = _print_keepset(
        local / "mind_api" / "state", tmp_path / "empty",
        "--keep-repo", _fwd(elsewhere),
    )
    pids = _pids(res.get("KEEPSET_PIDS"))
    assert {"1111", "2222", "7777", "8888"} <= pids, f"--keep-repo failed: {pids}"


def test_vanished_pidfile_not_in_keepset(tmp_path):
    """A deployment whose daemon.pid was deleted (the teardown-orphan failure
    mode) is correctly ABSENT from the keep-set, so a true orphan stays
    sweepable."""
    local = _make_repo(tmp_path, "repo_local", 1111, 2222)
    gone = _make_repo(tmp_path, "repo_gone", 3333, 4444)
    (gone / "mind_api" / "state" / "daemon.pid").unlink()
    (gone / "mind_api" / "state" / "daemon.parent.pid").unlink()
    res = _print_keepset(local / "mind_api" / "state", tmp_path)
    pids = _pids(res.get("KEEPSET_PIDS"))
    assert "3333" not in pids and "4444" not in pids, (
        f"vanished daemon still protected (would never be reaped): {pids}"
    )
    assert {"1111", "2222"} <= pids


def test_child_only_repo_lists_child_in_children(tmp_path):
    """A repo with daemon.pid but no daemon.parent.pid contributes its child
    to KEEPSET_CHILDREN (so the Windows path can derive the live parent)."""
    local = _make_repo(tmp_path, "repo_local", 1111, 2222)
    _make_repo(tmp_path, "repo_childonly", 9999, parent=None)
    res = _print_keepset(local / "mind_api" / "state", tmp_path)
    children = _pids(res.get("KEEPSET_CHILDREN"))
    assert "9999" in children, f"child-only repo child missing from children: {children}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
