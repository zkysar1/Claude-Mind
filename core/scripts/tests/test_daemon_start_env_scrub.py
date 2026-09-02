"""test_daemon_start_env_scrub.py —  regression tests.

The launcher twin of test_runtime_spawn_env_scrub.py. That file pins the scrub
on the WRAPPER auto-respawn path (_runtime.sh rt_spawn); this one pins it on
the DIRECT launcher path (mind-api-start.sh), which had no scrub at all.

INCIDENT (2026-09-02, alpha, DESKTOP-O91DLK2). mind-api-start.sh's shared-
runtime claim gate refuses a spawn whose parent is pytest; MIND_ALLOW_SHARED_
DAEMON_FROM_TEST=1 is the sanctioned opt-in past it. The opt-in lifted the
REFUSAL and, because the launcher spawned with the caller env intact, it lifted
the clean ENVIRONMENT with it. Seven --restart recycles from inside pytest
(00:33:20-00:34:27Z) each spawned the shared daemon carrying guard-955's
mandatory STORAGE_BACKEND=local; the daemon resolved LocalBackend on an
own-cloud box and every daemon-mediated world write from that box stayed on the
local mirror until the clean restart at 06:52:47Z — a goal filing, a goal close
plus outcome note, four board posts, a guardrail increment and two
override-ledger entries.

guard-2617 is why an inherited value is decisive rather than advisory: the
DAEMON resolves the backend, and mind_api's _load_env_local uses setdefault
("explicit launch env wins"), so an inherited STORAGE_BACKEND BLOCKS .env.local.

THE PREDICATE IS `PYTEST_CURRENT_TEST`, NOT THE HATCH, and both halves are
pinned below. The hatch says "this test may use the shared runtime dir"; it
says nothing about the environment, so keying the scrub to it would leave the
identical hole for any other test that reaches this launcher. Keying to
"is my parent a test" is what mind-api-start.sh's own gate comment already
argues ("that comment explicitly exempts this script as 'a deliberate
operator', which is false when the parent process is a test").

test_operator_launch_is_not_scrubbed is the load-bearing NEGATIVE control
(guard-1220 — a predicate must reject as well as accept). Without it, a scrub
that fired unconditionally would pass every other assertion here while
silently breaking deliberate operator env-shaping, which _runtime.sh's
exemption comment protects on purpose.

STRATEGY. Run the REAL mind-api-start.sh with RUNTIME_DIR pointed at a tmp dir
(so the shared-runtime gate never fires and this box's live daemon is never
touched — kills go by known PIDs read from that empty dir) and with a fake
`python3`/`py` first on PATH that dumps its environment and exits. The launcher
then polls ~10s for a daemon that never publishes a port and gives up; that is
expected and its exit status is deliberately NOT asserted on. The artifact
under test is the environment the launcher handed the child.
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
START_SH = CORE_SCRIPTS / "mind-api-start.sh"

sys.path.insert(0, str(SCRIPT_DIR))
from _bash_helpers import BASH as GIT_BASH  # noqa: E402


def _to_bash_path(p) -> str:
    """C:\\a\\b -> /c/a/b for Git-Bash (msys) consumption."""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _run_launcher(env_overrides: dict) -> str:
    """Run the real launcher against an isolated RUNTIME_DIR with a fake
    interpreter on PATH; return the child's captured environment."""
    tmp = Path(tempfile.mkdtemp(prefix="daemon-start-scrub-"))
    rt_dir = tmp / "rt"
    rt_dir.mkdir(parents=True, exist_ok=True)
    bindir = tmp / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    capture = tmp / "captured.env"

    # Both names: _python_launcher prefers `py` where present and falls back to
    # python3, and this box has the py shim — so faking only one can leave the
    # real interpreter selected and actually start a daemon.
    for name in ("python3", "py"):
        fake = bindir / name
        fake.write_text(
            "#!/bin/bash\n" f'env > "{_to_bash_path(capture)}"\n',
            encoding="utf-8", newline="\n",
        )
        fake.chmod(0o755)

    env = dict(os.environ)
    # Never inherit this process's own pytest/session context by accident —
    # each case declares exactly what it wants.
    for k in list(env):
        if k.startswith(("PYTEST_", "MOTO_")) or k in (
            "STORAGE_BACKEND", "MIND_ALLOW_SHARED_DAEMON_FROM_TEST",
        ):
            env.pop(k, None)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["RUNTIME_DIR"] = str(rt_dir)
    env.update(env_overrides)

    subprocess.run(
        [GIT_BASH, _to_bash_path(START_SH)],
        capture_output=True, text=True, timeout=90,
        cwd=str(PROJECT_ROOT), env=env,
    )
    assert capture.exists() and capture.read_text(encoding="utf-8").strip(), (
        "fake interpreter never ran — the launcher did not reach its spawn "
        "site, so this case proves nothing (anti-vacuity floor, rb-245)"
    )
    return capture.read_text(encoding="utf-8")


def _env_keys(captured: str) -> set:
    return {ln.split("=", 1)[0] for ln in captured.splitlines() if "=" in ln}


STORAGE_KEYS = {
    "STORAGE_BACKEND", "STORAGE_S3_BUCKET", "STORAGE_DDB_SESSIONS_TABLE",
    "STORAGE_DDB_LOCK_TABLE", "ENVIRONMENT_ID", "MACHINE_ID", "MACHINE_MULTI",
    "OWNCLOUD_SYNC_INTERVAL", "OWNCLOUD_CACHE_TTL", "MIND_API_TOKEN",
    "MIND_API_BIND",
}


def test_pytest_parent_scrubs_storage_config():
    """The incident, reduced: a launcher spawn whose parent is pytest must not
    hand the daemon a STORAGE_BACKEND. Also asserts the sanctioned keeps."""
    captured = _run_launcher({
        "PYTEST_CURRENT_TEST": "core/scripts/tests/test_x.py::test_y",
        "STORAGE_BACKEND": "local",
        "STORAGE_S3_BUCKET": "pytest-fake-bucket",
        "ENVIRONMENT_ID": "test-env",
        "MACHINE_ID": "pytest-machine",
        "MIND_ALLOW_TMP_OWNCLOUD_PUT": "1",
        "MOTO_CALL_RESET_API": "false",
        "WORLD_PATH": "/tmp/some-world",
        "META_PATH": "/tmp/some-meta",
    })
    keys = _env_keys(captured)
    leaked = keys & STORAGE_KEYS
    assert not leaked, f"storage config reached the daemon: {leaked}"
    assert not {k for k in keys if k.startswith(("PYTEST_", "MOTO_"))}, (
        "test markers reached the daemon"
    )
    assert "MIND_ALLOW_TMP_OWNCLOUD_PUT" not in keys, (
        "tempdir-tripwire escape hatch reached the daemon"
    )
    # Sanctioned keeps: isolation + the documented path channel must survive.
    for keep in ("RUNTIME_DIR", "WORLD_PATH", "META_PATH"):
        assert keep in keys, f"sanctioned var {keep} was wrongly scrubbed"


def test_shared_daemon_hatch_does_not_lift_the_scrub():
    """MIND_ALLOW_SHARED_DAEMON_FROM_TEST=1 lifts the REFUSAL, never the
    scrub. This is the exact combination the incident ran under."""
    captured = _run_launcher({
        "PYTEST_CURRENT_TEST": "core/scripts/tests/test_daemon_orphan_prevention.py::test_z",
        "MIND_ALLOW_SHARED_DAEMON_FROM_TEST": "1",
        "STORAGE_BACKEND": "local",
    })
    assert "STORAGE_BACKEND" not in _env_keys(captured), (
        "the shared-daemon opt-in lifted the env scrub as well as the refusal "
        "— that is the g-115-8604 defect"
    )


def test_operator_launch_is_not_scrubbed():
    """NEGATIVE CONTROL (guard-1220). With no pytest parent the launcher IS a
    deliberate operator, and hand-set env must still shape the daemon — the
    half of _runtime.sh's 'deliberate operator' exemption that was always
    true. A scrub that fired unconditionally would pass both tests above and
    silently break this."""
    captured = _run_launcher({"STORAGE_BACKEND": "local"})
    keys = _env_keys(captured)
    assert "STORAGE_BACKEND" in keys, (
        "an operator launch was scrubbed — the predicate no longer "
        "discriminates test parents from deliberate operators"
    )
    assert "PATH" in keys, "sanity: capture missing PATH — harness broken"
