"""test_runtime_spawn_env_scrub.py —  regression tests.

Verifies rt_spawn (core/scripts/_runtime.sh) canonicalizes the daemon
environment at the wrapper auto-respawn chokepoint. Incident (2026-07-16,
cc-04): a background pytest suite's wrapper call auto-respawned the
production daemon INHERITING the pytest environment — STORAGE_BACKEND=local
+ MIND_ALLOW_TMP_OWNCLOUD_PUT=1 + PYTEST_CURRENT_TEST — and mind_api's
_load_env_local uses setdefault ("explicit launch env wins"), so the
inherited vars BLOCKED .env.local and the live daemon served LocalBackend
for ~6 minutes (split-brain, tempdir tripwire dormant).

The fix scrubs, inside rt_spawn's spawn subshell only:
  - test markers (PYTEST_*, MOTO_*) + MIND_ALLOW_TMP_OWNCLOUD_PUT
  - every storage/config key the daemon self-resolves from .env.local
    (_N3_ALLOWED_EXACT minus RUNTIME_DIR)
while KEEPING RUNTIME_DIR (sanctioned per-test daemon isolation) and the
path context (WORLD_PATH/META_PATH arrive from the spawning shell by
design — see _load_env_local docstring).

Test strategy mirrors test_runtime_staleness_autorestart.py (rb-919): source
the REAL _runtime.sh, override only the leaf primitives (rt_python_launcher
→ a fake launcher that dumps its environ to a capture file; rt_daemon_kill →
no-op), run the REAL rt_spawn body, and assert on the captured child env.

3 cases:
  1. test-markers-scrubbed      — PYTEST_*/MOTO_*/tripwire absent in child
  2. storage-config-scrubbed    — STORAGE_*/ENVIRONMENT_ID/MACHINE_ID absent
  3. sanctioned-vars-preserved  — RUNTIME_DIR/WORLD_PATH/META_PATH survive
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
RUNTIME_SH = CORE_SCRIPTS / "_runtime.sh"


def _to_bash_path(p) -> str:
    """C:\\a\\b -> /c/a/b for Git-Bash (msys) consumption."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _run_spawn(extra_exports: str) -> str:
    """Source the real _runtime.sh, stub the leaves, run the REAL rt_spawn,
    and return the spawned child's captured environment (env(1) output)."""
    tmp = Path(tempfile.mkdtemp(prefix="rt-spawn-scrub-"))
    capture = tmp / "captured.env"
    fake = tmp / "fake-python3"
    # The fake launcher stands in for `python3 -m mind_api.src`: it ignores
    # its args and dumps the environment rt_spawn handed it.
    fake.write_text(
        "#!/bin/bash\n"
        f'env > "{_to_bash_path(capture)}"\n',
        encoding="utf-8", newline="\n",
    )
    fake.chmod(0o755)
    harness = (
        "set -uo pipefail\n"
        f'export PROJECT_ROOT="{_to_bash_path(PROJECT_ROOT)}"\n'
        # RT_DIR before source: RT_SPAWN_LOG derives from it at source time.
        f'export RT_DIR="{_to_bash_path(tmp)}/rt"\n'
        f'source "{_to_bash_path(RUNTIME_SH)}"\n'
        "rt_daemon_kill() { :; }\n"
        f'rt_python_launcher() {{ echo "{_to_bash_path(fake)}"; }}\n'
        + extra_exports
        + "rt_spawn\n"
        # rt_spawn backgrounds + disowns the child — poll for the capture.
        "for _ in $(seq 1 50); do\n"
        f'  [ -s "{_to_bash_path(capture)}" ] && break\n'
        "  sleep 0.1\n"
        "done\n"
        f'cat "{_to_bash_path(capture)}"\n'
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sh", delete=False, newline="\n"
    ) as fh:
        fh.write(harness)
        script = fh.name
    try:
        r = subprocess.run(
            [GIT_BASH, _to_bash_path(script)],
            capture_output=True, text=True, timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        assert r.returncode == 0, f"harness failed:\n{r.stdout}\n{r.stderr}"
        assert r.stdout.strip(), (
            f"empty env capture — fake launcher never ran:\n{r.stderr}"
        )
        return r.stdout
    finally:
        os.unlink(script)


def _env_keys(captured: str) -> set:
    return {
        line.split("=", 1)[0]
        for line in captured.splitlines() if "=" in line
    }


def test_test_markers_scrubbed():
    """PYTEST_*/MOTO_* wildcards + the tempdir-tripwire escape hatch must not
    reach the spawned daemon (the exact incident vars)."""
    captured = _run_spawn(
        'export PYTEST_CURRENT_TEST="core/scripts/tests/test_x.py::test_y"\n'
        'export PYTEST_VERSION="8.0.0"\n'
        'export PYTEST_XDIST_WORKER="gw0"\n'
        'export MOTO_CALL_RESET_API="false"\n'
        'export MIND_ALLOW_TMP_OWNCLOUD_PUT="1"\n'
    )
    keys = _env_keys(captured)
    leaked = {k for k in keys if k.startswith(("PYTEST_", "MOTO_"))}
    assert not leaked, f"test markers leaked into daemon env: {leaked}"
    assert "MIND_ALLOW_TMP_OWNCLOUD_PUT" not in keys, (
        "tempdir-tripwire escape hatch leaked into daemon env"
    )
    assert "PATH" in keys, "sanity: capture missing PATH — harness broken"


def test_storage_config_scrubbed():
    """Every daemon-self-resolved storage key (_N3_ALLOWED_EXACT minus
    RUNTIME_DIR) is scrubbed so .env.local is the single source of truth —
    _load_env_local's setdefault would otherwise let the poker's value win."""
    captured = _run_spawn(
        'export STORAGE_BACKEND="local"\n'
        'export STORAGE_S3_BUCKET="pytest-fake-bucket"\n'
        'export STORAGE_DDB_SESSIONS_TABLE="fake-sessions"\n'
        'export STORAGE_DDB_LOCK_TABLE="fake-locks"\n'
        'export ENVIRONMENT_ID="test-env"\n'
        'export MACHINE_ID="pytest-machine"\n'
        'export MACHINE_MULTI="1"\n'
        'export OWNCLOUD_SYNC_INTERVAL="1"\n'
        'export OWNCLOUD_CACHE_TTL="1"\n'
        'export MIND_API_TOKEN="fake-token"\n'
        'export MIND_API_BIND="0.0.0.0"\n'
        'export MIND_API_PORT="59999"\n'
    )
    keys = _env_keys(captured)
    scrub_set = {
        "STORAGE_BACKEND", "STORAGE_S3_BUCKET", "STORAGE_DDB_SESSIONS_TABLE",
        "STORAGE_DDB_LOCK_TABLE", "ENVIRONMENT_ID", "MACHINE_ID",
        "MACHINE_MULTI", "OWNCLOUD_SYNC_INTERVAL", "OWNCLOUD_CACHE_TTL",
        "MIND_API_TOKEN", "MIND_API_BIND", "MIND_API_PORT",
    }
    leaked = keys & scrub_set
    assert not leaked, f"storage config leaked into daemon env: {leaked}"


def test_sanctioned_vars_preserved():
    """RUNTIME_DIR (per-test daemon isolation) and WORLD_PATH/META_PATH (the
    documented spawn-shell path channel) MUST survive the scrub."""
    captured = _run_spawn(
        'export RUNTIME_DIR="/tmp/rt-isolated-test"\n'
        'export WORLD_PATH="/tmp/some-world"\n'
        'export META_PATH="/tmp/some-meta"\n'
        'export STORAGE_BACKEND="local"\n'  # control: scrubbed alongside
    )
    keys = _env_keys(captured)
    for keep in ("RUNTIME_DIR", "WORLD_PATH", "META_PATH"):
        assert keep in keys, f"sanctioned var {keep} was wrongly scrubbed"
    assert "STORAGE_BACKEND" not in keys, (
        "control failed: STORAGE_BACKEND should be scrubbed in this case too"
    )


def test_git_repo_override_vars_scrubbed():
    """git's repository-override variables must not reach the spawned daemon.

    git exports them into HOOK processes, and the private-index commit recipe
    (rb-9959) exports GIT_INDEX_FILE by hand. Observed 2026-09-02 (alpha,
    DESKTOP-O91DLK2): a pre-commit gate poked a wrapper mid-commit, rt_spawn
    respawned the daemon carrying GIT_INDEX_FILE=<tmp index>, the temp file
    was deleted seconds later, and every git call the daemon made afterwards
    saw a MISSING index -- the uncommitted-work gate reported every tracked
    framework file dirty while `git status` in a shell was clean, refusing
    every goal close on the box until the daemon was restarted clean.
    """
    captured = _run_spawn(
        'export GIT_INDEX_FILE="/tmp/tmp.private-index"\n'
        'export GIT_DIR=".git"\n'
        'export GIT_WORK_TREE="."\n'
        'export GIT_PREFIX="core/scripts/"\n'
        'export GIT_COMMON_DIR=".git"\n'
    )
    keys = _env_keys(captured)
    leaked = {k for k in keys if k in (
        "GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_PREFIX", "GIT_COMMON_DIR",
    )}
    assert not leaked, f"git repo-override vars leaked into daemon env: {leaked}"
    assert "PATH" in keys, "sanity: capture missing PATH -- harness broken"
