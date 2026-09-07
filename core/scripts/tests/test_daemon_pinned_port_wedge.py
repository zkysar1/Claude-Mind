""" — the pinned-port self-wedge must fail LOUDLY, naming the recovery.

MIND_API_PORT pins the daemon's listen port (g-358-62). That pin is correct: an
OS-assigned port turns over on every recycle and a client holding the old one
fails OPEN. But it converts ONE unpublished orphan into a PERMANENT wedge —
every later restart binds the same port, hits EADDRINUSE and dies.

Measured 2026-09-06 (echo, cc-03, Linux 6.8.0-138-generic; guard-6154): seven
consecutive `bind_failed` on port 19003 behind a 27-minute-old orphan, with
daemon.pid / daemon.parent.pid / daemon.port all missing, while the wrapper said
only "did not become ready within 10s". The framework is daemon-only, so that is
a total work stoppage AND the agent cannot use the daemon to diagnose the
daemon. The caller retried blind seven times.

WHY NO REAL DAEMON IS SPAWNED HERE. Two independent hazards, both live on this
box:
  * Under pytest, `mind-api-start.sh` scrubs MIND_API_PORT from the spawn env so
    the daemon "resolves it for itself" — from `.env.local`, i.e. the LIVE
    pinned port. A test that let the daemon start could therefore bind or fight
    the production port no matter what it put in the child env.
  * A spawn with RUNTIME_DIR unset claims the shared daemon.pid/port
    (g-115-3329, guard-955).
So the launcher is stubbed to fail ONLY for `-m mind_api.src` and to exec the
real interpreter for everything else. The wrapper's failure path — the file
reads, the log scan, the message — is exercised for real; only the daemon is
absent. RUNTIME_DIR is isolated per test and STORAGE_BACKEND is pinned local.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
from _runtime_bash import bash_cmd  # noqa: E402

START_SH = PROJECT_ROOT / "core" / "scripts" / "mind-api-start.sh"

BIND_FAILED = (
    '{"ts": "2026-09-06T20:29:31", "event": "bind_failed", "version": "2.12.63", '
    '"port": %d, "error": "OSError(98, \'Address already in use\')"}'
)


def _stub_launcher(tmp_path: Path) -> Path:
    """A PATH dir whose python3 refuses `-m mind_api.src` and passes the rest."""
    real = shutil.which("python3") or sys.executable
    binv = tmp_path / "stub-bin"
    binv.mkdir(parents=True, exist_ok=True)
    stub = binv / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "mind_api.src" ]; then exit 1; fi\n'
        "done\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return binv


def _run_start(tmp_path: Path, daemon_log):
    rt = tmp_path / "state"
    rt.mkdir(parents=True, exist_ok=True)
    if daemon_log is not None:
        (rt / "daemon.log").write_text(daemon_log + "\n", encoding="utf-8")

    # The published pair is ABSENT — the guard-5681 shape.
    assert not (rt / "daemon.pid").exists()
    assert not (rt / "daemon.port").exists()

    env = dict(os.environ)
    env["RUNTIME_DIR"] = str(rt)
    env["STORAGE_BACKEND"] = "local"          # guard-955
    env["PATH"] = f"{_stub_launcher(tmp_path)}{os.pathsep}" + env.get("PATH", "")
    proc = subprocess.run(
        bash_cmd(str(START_SH)),
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, text=True, timeout=180,
    )
    return proc, rt


def test_wedged_pinned_port_names_the_port_and_the_recovery(tmp_path):
    """The reproduction: orphan holds the pinned port, published files gone."""
    proc, rt = _run_start(tmp_path, BIND_FAILED % 19003)

    assert proc.returncode != 0, "a wedged start must not report success"
    err = proc.stderr

    # Names the wedged port — not just a generic timeout.
    assert "19003" in err, f"diagnostic must name the wedged port; got:\n{err}"
    assert "WEDGED" in err.upper(), f"diagnostic must say the port is wedged; got:\n{err}"

    # Names the recovery, which is what breaks the blind-retry loop.
    assert "daemon-orphan-sweep.sh" in err, f"must name the recovery tool; got:\n{err}"
    assert "--clean" in err, f"must name the reaping flag; got:\n{err}"

    # Tells the caller retrying is futile — the 7-retry behaviour this fixes.
    assert "NOT help" in err, f"must say retrying will not help; got:\n{err}"

    # It must NOT have published a pid/port for a daemon that never started.
    assert not (rt / "daemon.port").exists()
    assert not (rt / "daemon.pid").exists()


def test_ordinary_start_failure_does_not_claim_a_wedge(tmp_path):
    """Control: no bind_failed in the log => no wedge claim.

    Without this the diagnostic could fire on every failed start and the wedge
    message would carry no information (guard-2499: a detector that always fires
    is indistinguishable from one that is broken).
    """
    proc, _rt = _run_start(tmp_path, '{"ts": "2026-09-06T20:29:25", "event": "stopped"}')

    assert proc.returncode != 0
    err = proc.stderr
    assert "did not become ready" in err, f"expected the generic message; got:\n{err}"
    assert "WEDGED" not in err.upper(), f"must NOT claim a wedge with no bind_failed; got:\n{err}"
    assert "19003" not in err


def test_start_wrapper_still_refuses_an_implicit_system_wide_sweep():
    """The recovery is NAMED, never fired implicitly.

    `_sweep_orphan_daemons` with empty args kills every mind_api.src process on
    the box and collides with sibling deployments; the wrapper documents that
    refusal. This pins it so a future 'just auto-reap it' change has to confront
    the reason rather than silently deleting it.
    """
    text = START_SH.read_text(encoding="utf-8")
    assert "DELIBERATELY no implicit" in text, "the documented refusal must survive"

    # Substring-matching the whole file cannot work: the refusal COMMENT quotes
    # the very call it refuses. Judge executable lines only, and exempt the
    # function definition itself.
    invocations = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "_sweep_orphan_daemons" not in stripped:
            continue
        if stripped.startswith("_sweep_orphan_daemons()"):
            continue          # the definition, not a call
        invocations.append(stripped)

    assert not invocations, (
        "mind-api-start.sh must not invoke _sweep_orphan_daemons — an empty-args "
        "sweep kills every mind_api.src process on the box, including sibling "
        f"deployments' live daemons. Found: {invocations}"
    )
