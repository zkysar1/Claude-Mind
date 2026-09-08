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


def _stub_launcher(tmp_path: Path, counter: Path | None = None) -> Path:
    """A PATH dir whose python3 refuses `-m mind_api.src` and passes the rest.

    When `counter` is given the stub appends one line per refused daemon spawn,
    so a caller can count how many times the wrapper actually tried to start the
    daemon — that count is how the single-retry guarantee is measured.
    """
    real = shutil.which("python3") or sys.executable
    binv = tmp_path / "stub-bin"
    binv.mkdir(parents=True, exist_ok=True)
    stub = binv / "python3"
    tally = f'echo x >> "{counter}"; ' if counter is not None else ""
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do\n'
        f'  if [ "$a" = "mind_api.src" ]; then {tally}exit 1; fi\n'
        "done\n"
        f'exec "{real}" "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return binv


def _run_start(tmp_path: Path, daemon_log, extra_env=None, counter: Path | None = None):
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
    env["PATH"] = f"{_stub_launcher(tmp_path, counter)}{os.pathsep}" + env.get("PATH", "")
    # Never inherit an opt-in from the box running the suite.
    env.pop("MIND_API_AUTO_REAP", None)
    env.pop("MIND_API_AUTO_REAP_ATTEMPTED", None)
    if extra_env:
        env.update(extra_env)
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


# ──  outcome 2: the opt-in auto-reap ────────────────────────────────


def _stub_sweeper(tmp_path: Path, marker: Path) -> Path:
    """A stand-in for daemon-orphan-sweep.sh that records the call instead of reaping.

    The real sweeper with `--clean` kills orphaned mind_api.src processes on the
    whole box. Running it from a test would put this box's LIVE fleet daemon in
    the blast radius of a unit test, so the wrapper reads its sweeper path from
    MIND_API_ORPHAN_SWEEP and this stub takes that slot. What is under test is
    the wrapper's control flow — did it sweep, did it retry exactly once, did it
    release the lock — not the sweeper's reaping logic, which has its own tests.
    """
    s = tmp_path / "stub-sweep.sh"
    s.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{marker}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    s.chmod(0o755)
    return s


def test_auto_reap_is_off_by_default(tmp_path):
    """Unset MIND_API_AUTO_REAP must leave the failure path exactly as it was.

    The opt-in is worthless if it changes the default: a box that never sets the
    var must still get the loud named-recovery message and nothing else. This is
    the control for every assertion in the opt-in test below.
    """
    marker = tmp_path / "swept.txt"
    proc, _rt = _run_start(
        tmp_path, BIND_FAILED % 19003,
        extra_env={"MIND_API_ORPHAN_SWEEP": str(_stub_sweeper(tmp_path, marker))},
    )

    assert proc.returncode != 0
    err = proc.stderr
    assert "WEDGED" in err.upper(), f"the default diagnostic must survive; got:\n{err}"
    assert "daemon-orphan-sweep.sh" in err, "the default must still NAME the recovery"

    # The reap must not have fired, and its two messages must not appear.
    assert not marker.exists(), "the sweeper ran without MIND_API_AUTO_REAP=1"
    assert "MIND_API_AUTO_REAP" not in err, (
        f"the default path must not mention the opt-in; got:\n{err}"
    )


def test_auto_reap_opt_in_sweeps_retries_once_and_never_leaks_the_spawn_lock(tmp_path):
    """MIND_API_AUTO_REAP=1: sweep, retry EXACTLY once, hold the lock invariant.

    Three properties in one run, because they are only true together:

    * the sweeper is invoked (the wedge is actually acted on);
    * the daemon spawn is attempted exactly TWICE — the original plus one retry.
      The recursion guard is the whole safety story: without it a permanently
      wedged port would re-exec forever;
    * the wrapper mutex is NOT leaked. `exec` does not run the EXIT trap, and
      this script's only trap is `_release_spawn_lock` (guard-5820), so a naive
      re-exec would leave `daemon.wrapper.lock` behind and every later spawn
      would block the full 10s waiting for a holder that no longer exists.

    The stub launcher refuses every spawn, so the retry fails too — which is the
    case worth pinning: the reader must be told the reap already happened rather
    than being sent to run it again.
    """
    marker = tmp_path / "swept.txt"
    counter = tmp_path / "spawn-attempts.txt"
    proc, rt = _run_start(
        tmp_path, BIND_FAILED % 19003,
        extra_env={
            "MIND_API_AUTO_REAP": "1",
            "MIND_API_ORPHAN_SWEEP": str(_stub_sweeper(tmp_path, marker)),
        },
        counter=counter,
    )
    err = proc.stderr

    assert marker.exists(), f"the sweeper was never invoked; stderr:\n{err}"
    assert "--clean" in marker.read_text(encoding="utf-8"), "the sweeper must be reaping, not reporting"

    attempts = counter.read_text(encoding="utf-8").split() if counter.exists() else []
    assert len(attempts) == 2, (
        f"expected exactly 2 daemon spawn attempts (original + ONE retry), got "
        f"{len(attempts)}; stderr:\n{err}"
    )

    # The exec hazard, measured directly rather than inferred from the message.
    assert not (rt / "daemon.wrapper.lock").exists(), (
        "the spawn lock leaked across the re-exec — `exec` skips the EXIT trap, "
        "so the lock must be released explicitly before it"
    )
    # A leaked lock also changes what the retry says; pin the symptom too.
    assert "concurrent spawn did not publish" not in err, (
        f"the retry blocked on a leaked wrapper mutex; got:\n{err}"
    )

    # Still wedged after the reap: say so, do not send the reader round again.
    assert proc.returncode != 0
    assert "already reaped and retried once" in err, (
        f"a post-reap wedge must report that the reap already ran; got:\n{err}"
    )
