#!/usr/bin/env python3
"""Tests for core/scripts/_runtime_bash.py ().

resolve_bash()/BASH pin a usable bash (Git Bash, not the System32 WSL stub)
for the 13 Windows-broken ``subprocess.run(['bash', ...])`` callsites migrated
in g-115-900. bash_cmd() additionally enforces the ``.as_posix()`` script-path
convention (guard-581). The final test reproduces the audit's failing probe
(g-115-863 / g-115-789): a Windows-style absolute path that the System32 WSL
bash rejects with rc=127 and Git Bash runs with rc=0.
"""
import subprocess
import sys
from pathlib import Path

import pytest

# Importable both under pytest (conftest adds core/scripts) and via ad-hoc
# ``py -3 test_runtime_bash_resolution.py`` (no conftest) — mirror the
# SCRIPT_DIR insert the other refactored tests carry (conftest.py:74-76).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import BASH, bash_cmd, resolve_bash  # noqa: E402


def test_resolve_bash_returns_existing_path():
    resolved = resolve_bash()
    assert isinstance(resolved, str) and resolved
    # On any configured host (Windows Git Bash, or POSIX with bash on PATH)
    # the resolver returns a real file. The bare "bash" sentinel is the only
    # non-path return, and only when no bash exists anywhere — never a
    # configured CI/dev box.
    assert Path(resolved).exists() or resolved == "bash"


def test_bash_constant_is_resolved_once():
    # BASH is computed at import time; it must equal a fresh resolve_bash().
    assert BASH == resolve_bash()


def test_bash_cmd_prepends_resolved_bash():
    cmd = bash_cmd("core/scripts/world-cat.sh", "file.json")
    assert cmd[0] == BASH
    assert cmd[1] == "core/scripts/world-cat.sh"
    assert cmd[2] == "file.json"


def test_bash_cmd_posix_normalizes_path():
    # guard-581: a Path is emitted via .as_posix(), never str(WindowsPath)
    # whose backslashes bash treats as escapes and strips.
    cmd = bash_cmd(Path("foo") / "bar" / "baz.sh")
    assert "\\" not in cmd[1]
    assert cmd[1].endswith("foo/bar/baz.sh")


def test_bash_cmd_stringifies_args():
    cmd = bash_cmd("s.sh", 5, "--flag", 0)
    assert cmd[2:] == ["5", "--flag", "0"]


def test_bash_runs_windows_style_path(tmp_path):
    # Regression for : subprocess.run([<resolved bash>, <abs path>])
    # must succeed. The System32 WSL stub returns rc=127 on a Windows-side
    # absolute path; Git Bash (what resolve_bash picks) returns rc=0.
    if BASH == "bash":
        pytest.skip("no real bash resolved on this host")
    script = tmp_path / "probe.sh"
    script.write_text("echo ok\n")
    r = subprocess.run(
        [BASH, script.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr!r}"
    assert "ok" in r.stdout
