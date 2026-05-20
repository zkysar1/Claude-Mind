"""Daemon-down behavior tests.

Post-cutover, wrappers have no CLI fallback. When the daemon is
unreachable, the wrapper must exit non-zero with a clear error
message on stderr.

These tests verify that contract WITHOUT spawning a real daemon:
RT_NO_AUTOSPAWN=1 suppresses both spawn entrypoints
(rt_ensure_running and rt_try_autospawn in _runtime.sh), so the
wrapper observes a genuine daemon-down state and takes its rc=3
failure path deterministically. RT_DIR still points at an isolated
gitignored throwaway dir as defense-in-depth (no PID/port file).

Before 2026-05-15 this suite set only RT_DIR=<repo-root path> and
relied on "no daemon discovered" — but the wrapper auto-spawned a
real `python -m mind_api.src`, leaving orphan processes + repo-root
cruft and passing for a spawn-race reason rather than the
documented contract. See _runtime.sh RT_NO_AUTOSPAWN guard.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Throwaway runtime dir for the daemon-down wrappers. MUST live under the
# gitignored core/scripts/tests/_tmp_*/ convention (.gitignore): the wrapper's
# rt_spawn() does `mkdir -p "$RT_DIR"` + writes spawn.log + background-spawns
# `python -m mind_api.src`, so a repo-root path here regenerated committable
# cruft on every test run (was REPO_ROOT/.runtime-nonexistent-test until
# 2026-05-15 — untracked, NOT gitignored, swept hourly by the live agents'
# test circuits). teardown_module rmtree's it so nothing survives the session.
_RT_DIR = REPO_ROOT / "core" / "scripts" / "tests" / "_tmp_runtime_daemon_down"


def teardown_module(module):  # noqa: ARG001 — pytest module-teardown hook
    shutil.rmtree(_RT_DIR, ignore_errors=True)


def _bash() -> str:
    return shutil.which("bash") or "bash"


def _run_wrapper(wrapper_name: str, args: list[str], *,
                 agent: str = "alpha", stdin: str = "") -> tuple[int, str, str]:
    """Run a wrapper with RT_DIR pointing at a bogus dir (no daemon)."""
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MSYS_NO_PATHCONV"] = "1"
    # Suppress BOTH spawn entrypoints so the wrapper observes a genuine
    # daemon-down state without launching an orphan daemon (the actual
    # contract under test — see module docstring + _runtime.sh guard).
    env["RT_NO_AUTOSPAWN"] = "1"
    # Throwaway runtime dir, no pre-existing daemon (gitignored + rmtree'd
    # by teardown_module — defense-in-depth; nothing writes here now).
    env["RT_DIR"] = str(_RT_DIR)
    wrapper = REPO_ROOT / "core" / "scripts" / wrapper_name
    proc = subprocess.run(
        [_bash(), wrapper.as_posix(), *args],
        env=env, capture_output=True, text=True, check=False,
        input=stdin if stdin else None,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_read_wrapper_exits_nonzero_when_daemon_down():
    """aspirations-read.sh must exit non-zero when daemon is unreachable."""
    rc, out, err = _run_wrapper("aspirations-read.sh", ["--active-compact"])
    assert rc != 0, (
        f"expected non-zero exit when daemon is down, got rc={rc}\n"
        f"stdout={out!r}\nstderr={err!r}"
    )


def test_write_wrapper_exits_nonzero_when_daemon_down():
    """aspirations-update.sh must exit non-zero when daemon is unreachable."""
    rc, out, err = _run_wrapper(
        "aspirations-update.sh",
        ["asp-001", "title", "should-not-persist"],
    )
    assert rc != 0, (
        f"expected non-zero exit when daemon is down, got rc={rc}\n"
        f"stdout={out!r}\nstderr={err!r}"
    )


def test_daemon_down_stderr_contains_diagnostic():
    """Wrapper stderr must contain a useful diagnostic when daemon is down."""
    rc, out, err = _run_wrapper("aspirations-read.sh", ["--summary"])
    assert rc != 0
    lower_err = err.lower()
    # The wrapper should mention the daemon being unreachable or not running
    has_diagnostic = any(phrase in lower_err for phrase in [
        "daemon", "not running", "unreachable", "connection refused",
        "no daemon", "could not connect", "port", "runtime",
    ])
    assert has_diagnostic, (
        f"stderr should contain a diagnostic about daemon being down.\n"
        f"stderr={err!r}"
    )
