"""test_shared_runtime_claim_gate.py —  regression test.

Pins the shared-runtime claim gate at BOTH daemon spawn chokepoints:

  * ``core/scripts/mind-api-start.sh``  (direct / hook / ``/start`` path)
  * ``core/scripts/_runtime.sh``        (``rt_spawn`` wrapper auto-spawn path)

The defect (observed 2026-07-26): a test that touches any daemon-backed wrapper
reaches one of these paths with no runtime isolation, which force-kills the LIVE
daemon and writes the test process's pid/port into the shared
``PROJECT_ROOT/mind_api/state`` — repointing every agent on the box at whatever
world the test resolved. ``test_post_state_update_metric_gate_category.py`` did
exactly this; its tmp ``local-paths.conf`` did NOT isolate it, because
``.mind-data/`` outranks the conf in the resolution chain.

``rt_spawn`` already scrubbed test env off the respawned daemon (g-115-2378),
but scrubbing is fail-OPEN: the shared port is still claimed and the live daemon
still dies — only the replacement's environment is clean. These gates fail
CLOSED instead.

Design note — why the condition compares a PATH, not env-var names: the two
chokepoints take their isolation override from DIFFERENT variables
(``RT_DIR`` in _runtime.sh, which is what ``_daemon_fixture.py`` sets;
``RUNTIME_DIR`` in mind-api-start.sh). An earlier draft keyed on
``-z "$RUNTIME_DIR"`` and would have refused every correctly-isolated fixture
test. ``test_isolated_runtime_is_allowed`` is the regression pin for that.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
START_SH = CORE_SCRIPTS / "mind-api-start.sh"
RUNTIME_SH = CORE_SCRIPTS / "_runtime.sh"
SHARED_STATE = PROJECT_ROOT / "mind_api" / "state"


def _clean_env(**overrides) -> dict:
    """Env with every runtime-isolation override removed, then re-applied.

    PYTEST_CURRENT_TEST is left INTACT — this test runs under pytest, so the
    real signal the gate keys on is present naturally. That is the point: the
    test is its own fixture for the condition under test.
    """
    env = dict(os.environ)
    for k in ("RT_DIR", "RUNTIME_DIR", "MIND_ALLOW_SHARED_DAEMON_FROM_TEST"):
        env.pop(k, None)
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def _daemon_identity() -> tuple[str, str]:
    """(pid, port) of the live daemon, or ('','') when none is running."""
    def _read(name: str) -> str:
        p = SHARED_STATE / name
        try:
            return p.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
    return _read("daemon.pid"), _read("daemon.port")


def test_mind_api_start_refuses_shared_claim_from_pytest():
    """--restart from inside pytest must REFUSE before killing anything.

    --restart is required to reach the gate: the fast path exits 0 for a
    healthy daemon (deliberately, so a test poking a live daemon is untouched
    — see test_healthy_daemon_fast_path_is_untouched).
    """
    before = _daemon_identity()

    proc = subprocess.run(
        [BASH, str(START_SH), "--restart"],
        capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_clean_env(),
    )

    assert proc.returncode == 1, (
        f"expected refusal rc=1, got {proc.returncode}. stderr={proc.stderr[:600]}"
    )
    assert "REFUSED" in proc.stderr, f"no refusal message. stderr={proc.stderr[:600]}"
    assert str(SHARED_STATE) in proc.stderr.replace("\\", "/") or "SHARED" in proc.stderr

    # The whole point: the live daemon must be untouched.
    assert _daemon_identity() == before, (
        "live daemon was recycled by a refused hijack — the gate did not fire "
        "before _force_kill_tree"
    )


def test_healthy_daemon_fast_path_is_untouched():
    """A test poking a HEALTHY daemon (no --restart) must still succeed.

    This is the blast-radius pin: the gate sits AFTER the fast path so the
    common case (test invokes a wrapper, daemon is up) keeps working. Skipped
    when no live daemon is present, since then there is no fast path to take.
    """
    import pytest
    pid, port = _daemon_identity()
    if not pid or not port:
        pytest.skip("no live daemon on this box — fast path not exercisable")

    proc = subprocess.run(
        [BASH, str(START_SH)],
        capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_clean_env(),
    )
    assert proc.returncode == 0, (
        f"healthy-daemon poke should exit 0, got {proc.returncode}. "
        f"stderr={proc.stderr[:600]}"
    )
    assert "REFUSED" not in proc.stderr, (
        "gate fired on the harmless healthy-daemon fast path — it is placed too early"
    )
    assert _daemon_identity() == (pid, port)


def test_rt_spawn_refuses_shared_claim_from_pytest(tmp_path):
    """rt_spawn must refuse the shared claim and return 0 (never 1).

    Returning non-zero would trip `set -e` in a bare caller — the contract the
    launcher ABORT in rt_spawn documents. Non-destructive by construction:
    RT_PID_FILE points at a tmp file, so even a REGRESSED gate has no live PID
    to kill; the assertion still catches the regression via the log.
    """
    spawn_log = tmp_path / "spawn.log"
    pid_file = tmp_path / "daemon.pid"          # safety belt, see docstring

    script = (
        'set -u\n'
        f'source "{RUNTIME_SH.as_posix()}"\n'
        'rt_spawn\n'
        'echo "RC=$?"\n'
    )
    proc = subprocess.run(
        [BASH, "-c", script],
        capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT),
        env=_clean_env(RT_SPAWN_LOG=spawn_log, RT_PID_FILE=pid_file),
    )

    assert "RC=0" in proc.stdout, (
        f"rt_spawn must return 0 on refusal (set -e contract). stdout={proc.stdout[:400]} "
        f"stderr={proc.stderr[:400]}"
    )
    log = spawn_log.read_text(encoding="utf-8") if spawn_log.exists() else ""
    assert "REFUSED" in log, f"no refusal logged to spawn.log. log={log[:600]}"
    assert "attempting daemon start" not in log, (
        "rt_spawn proceeded to spawn despite the gate"
    )


def test_isolated_runtime_is_allowed(tmp_path):
    """A test that DOES isolate its runtime must pass the gate.

    Regression pin for the env-var-name bug: `_daemon_fixture.py` isolates via
    RT_DIR (never RUNTIME_DIR), so a gate keyed on RUNTIME_DIR alone would
    refuse every correctly-isolated fixture test. Evaluates the gate's own
    condition against the real RT_DIR resolution in _runtime.sh, without
    spawning a daemon.
    """
    script = (
        'set -u\n'
        f'source "{RUNTIME_SH.as_posix()}"\n'
        'if [ -n "${PYTEST_CURRENT_TEST:-}" ] '
        '&& [ "$(canon_dir "$RT_DIR")" = "$(canon_dir "$PROJECT_ROOT/mind_api/state")" ] '
        '&& [ "${MIND_ALLOW_SHARED_DAEMON_FROM_TEST:-}" != "1" ]; then '
        'echo VERDICT=REFUSE; else echo VERDICT=ALLOW; fi\n'
        'echo "RT_DIR=$RT_DIR"\n'
    )

    isolated = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_clean_env(RT_DIR=tmp_path),
    )
    assert "VERDICT=ALLOW" in isolated.stdout, (
        f"RT_DIR-isolated test was refused — fixture tests would break. "
        f"stdout={isolated.stdout[:400]}"
    )

    hatched = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT),
        env=_clean_env(MIND_ALLOW_SHARED_DAEMON_FROM_TEST="1"),
    )
    assert "VERDICT=ALLOW" in hatched.stdout, (
        f"deliberate-operator escape hatch did not work. stdout={hatched.stdout[:400]}"
    )

    shared = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_clean_env(),
    )
    assert "VERDICT=REFUSE" in shared.stdout, (
        f"unisolated shared claim was NOT refused. stdout={shared.stdout[:400]}"
    )


def test_both_chokepoints_carry_the_same_condition():
    """Drift guard: the two gates must stay in sync.

    They are deliberately duplicated (one is a sourced shell library, the other
    a standalone script with its own RT_DIR derivation), so nothing structural
    keeps them aligned. This pins the three clauses in both.
    """
    for path in (START_SH, RUNTIME_SH):
        src = path.read_text(encoding="utf-8")
        assert 'PYTEST_CURRENT_TEST' in src, f"{path.name}: lost the pytest clause"
        assert 'canon_dir "$RT_DIR"' in src, (
            f"{path.name}: lost the shared-dir comparison (or reverted to an "
            f"env-var-name check, which breaks RT_DIR-isolated fixture tests)"
        )
        assert 'canon_dir "$PROJECT_ROOT/mind_api/state"' in src, (
            f"{path.name}: compares RT_DIR against a RAW shared path. Both "
            f"operands must be canonicalized or a trailing slash / `..` "
            f"spelling of the shared dir slips the gate (g-115-3362)."
        )
        assert 'MIND_ALLOW_SHARED_DAEMON_FROM_TEST' in src, (
            f"{path.name}: lost the deliberate-operator escape hatch"
        )
        assert 'g-115-3329' in src, f"{path.name}: lost the traceability marker"


def test_noncanonical_spellings_of_shared_dir_are_refused(tmp_path):
    """A trailing slash or a `..` segment must not slip the gate ().

    The condition was an exact string compare, so every spelling of the shared
    dir other than the canonical one evaluated ALLOW -- and RT_DIR is operator-
    and fixture-supplied, i.e. exactly where alternate spellings come from. Runs
    the REAL rt_spawn per spelling; non-destructive by the same construction as
    test_rt_spawn_refuses_shared_claim_from_pytest (tmp RT_PID_FILE, and the
    gate returns before any kill).
    """
    shared = SHARED_STATE.as_posix()
    spellings = {
        "trailing slash": shared + "/",
        "doubled trailing slash": shared + "//",
        "`..` round trip": (PROJECT_ROOT / "mind_api" / ".." / "mind_api" / "state").as_posix(),
    }
    for label, spelling in spellings.items():
        spawn_log = tmp_path / f"spawn-{abs(hash(label))}.log"
        script = (
            'set -u\n'
            f'source "{RUNTIME_SH.as_posix()}"\n'
            'rt_spawn\n'
            'echo "RC=$?"\n'
        )
        proc = subprocess.run(
            [BASH, "-c", script],
            capture_output=True, text=True, timeout=60, cwd=str(PROJECT_ROOT),
            env=_clean_env(
                RT_DIR=spelling,
                RT_SPAWN_LOG=spawn_log,
                RT_PID_FILE=tmp_path / "daemon.pid",
            ),
        )
        assert "RC=0" in proc.stdout, (
            f"[{label}] rt_spawn must return 0 on refusal. stdout={proc.stdout[:400]}"
        )
        log = spawn_log.read_text(encoding="utf-8") if spawn_log.exists() else ""
        assert "REFUSED" in log, (
            f"[{label}] spelling {spelling!r} of the SHARED runtime dir slipped "
            f"the gate -- it would have killed the live daemon. log={log[:600]}"
        )
        assert "attempting daemon start" not in log, (
            f"[{label}] rt_spawn proceeded to spawn despite the gate"
        )


def test_canonicalization_does_not_over_match_isolated_dirs(tmp_path):
    """Negative control for the fix: a genuinely isolated RT_DIR still passes.

    Canonicalizing both operands must narrow nothing -- a tmp RT_DIR spelled
    with a trailing slash is still NOT the shared dir, and refusing it would
    break every correctly-isolated fixture test (the failure mode
    test_isolated_runtime_is_allowed exists to catch).
    """
    script = (
        'set -u\n'
        f'source "{RUNTIME_SH.as_posix()}"\n'
        'if [ -n "${PYTEST_CURRENT_TEST:-}" ] '
        '&& [ "$(canon_dir "$RT_DIR")" = "$(canon_dir "$PROJECT_ROOT/mind_api/state")" ] '
        '&& [ "${MIND_ALLOW_SHARED_DAEMON_FROM_TEST:-}" != "1" ]; then '
        'echo VERDICT=REFUSE; else echo VERDICT=ALLOW; fi\n'
    )
    proc = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_clean_env(RT_DIR=tmp_path.as_posix() + "/"),
    )
    assert "VERDICT=ALLOW" in proc.stdout, (
        f"canonicalization over-matched an isolated tmp dir. stdout={proc.stdout[:400]}"
    )


def test_refused_autospawn_surfaces_the_launcher_reason(tmp_path):
    """rt_try_autospawn must re-emit the launcher's stderr on failure.

    It ran `bash "$spawn_script" >/dev/null 2>&1`, so mind-api-start.sh's
    542-byte shared-runtime REFUSAL -- which names both fixes -- was discarded
    and the caller saw only rt_no_daemon_error's generic "daemon is
    unreachable". Drives the real function against a stub launcher via
    PROJECT_ROOT, so no daemon is touched.
    """
    stub_dir = tmp_path / "core" / "scripts"
    stub_dir.mkdir(parents=True)
    (stub_dir / "mind-api-start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo '[daemon-start] REFUSED: would claim SHARED runtime (g-115-3329)' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    script = (
        'set -u\n'
        f'source "{RUNTIME_SH.as_posix()}"\n'
        f'PROJECT_ROOT="{tmp_path.as_posix()}"\n'
        'rt_try_autospawn\n'
        'echo "RC=$?"\n'
    )
    proc = subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True, timeout=60,
        cwd=str(PROJECT_ROOT), env=_clean_env(RT_NO_AUTOSPAWN="0"),
    )
    assert "RC=1" in proc.stdout, (
        f"rt_try_autospawn must return 1 when the launcher fails. "
        f"stdout={proc.stdout[:400]} stderr={proc.stderr[:400]}"
    )
    assert "REFUSED" in proc.stderr, (
        f"launcher diagnostic was swallowed -- the caller cannot learn WHY the "
        f"spawn failed. stderr={proc.stderr[:600]}"
    )
