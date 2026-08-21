"""test_spawn_log_rotation.py — .

spawn.log had no rotation and no sweep entry, violating guard-586 ("every
hook-created sentinel, log, or telemetry file MUST be paired with EITHER a
stale-sweep entry ... OR an inline rotation policy at write time (size cap then
truncate-to-last-N-lines)"). Observed 1.31 GB on one box.

TWO THINGS ARE PINNED HERE, AND THE SECOND IS THE ONE THAT ROTS.

1. The helper behaves (``cap_log_file`` in core/scripts/_paths.sh).
2. The helper is CALLED from all four sites that touch spawn.log.

(2) exists because this repo's recurring defect is a correct component nobody
invokes — workers never pulled (g-306-233), the watchdog never ticked on a
worker (g-306-240), pre-edit-context-gate sat inert 59 days (g-115-3731),
heartbeat-tick was fixed and had no caller (g-306-227). In every case the
component's own tests stayed green throughout (guard-1943: pinning the writer
says nothing about the wiring). A helper this file proves correct, wired to
nothing, would leave spawn.log growing exactly as before with a green suite.

WHY COPYTRUNCATE AND NOT ``tail > tmp && mv tmp log`` — the trap this file keeps
shut. The obvious in-repo precedent (iteration-close.sh:2266) uses mv, and mv is
WRONG here: spawn.log is held open on fd 1 and fd 2 by a daemon that outlives
every shell involved (verified cc-08: flags=0102001, O_APPEND). mv swaps the
inode, so the daemon goes on appending to the orphaned one -- its output
disappears and the space is not reclaimed until restart. Measured under a live
O_APPEND writer: mv landed 5/40 subsequent lines, copytruncate 39/40.
``test_live_appender_survives_rotation`` is that measurement, and
``test_rotation_preserves_inode`` is its mechanism; a future refactor to mv
passes neither.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
PATHS_SH = CORE_SCRIPTS / "_paths.sh"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402


def _run_bash(body: str) -> subprocess.CompletedProcess:
    """Source _paths.sh and run `body`. Uses the real helper, never a copy.

    BASH, never a bare "bash" argv[0]: bare `bash` resolves via CreateProcess,
    which searches System32 BEFORE PATH on win32 and reaches the WSL launcher —
    it can hang forever against a wedged LxssManager (guard-580). PATHS_SH is
    passed as .as_posix() because bash silently strips the backslashes of a
    str(WindowsPath) (guard-581).
    """
    script = f'source "{PATHS_SH.as_posix()}" 2>/dev/null\n' + textwrap.dedent(body)
    return subprocess.run(
        [BASH, "-c", script], capture_output=True, text=True,
        cwd=str(PROJECT_ROOT), encoding="utf-8", errors="replace", timeout=120,
    )


# --------------------------------------------------------------------------
# 1. the helper behaves
# --------------------------------------------------------------------------

def test_under_cap_is_a_noop(tmp_path):
    """The load-bearing anti-vacuity case. A helper that truncated
    unconditionally would pass every over-cap assertion below, so without this
    the suite cannot tell a correct size cap from a broken one."""
    log = tmp_path / "spawn.log"
    log.write_text("\n".join(str(i) for i in range(500)) + "\n", encoding="utf-8")
    before = log.read_text(encoding="utf-8")
    r = _run_bash(f'cap_log_file "{log.as_posix()}" 10485760 2000')
    assert r.returncode == 0, r.stderr
    assert log.read_text(encoding="utf-8") == before, "under-cap file must be untouched"


def test_over_cap_truncates_and_keeps_the_newest_lines(tmp_path):
    log = tmp_path / "spawn.log"
    log.write_text("\n".join(str(i) for i in range(50000)) + "\n", encoding="utf-8")
    big = log.stat().st_size
    r = _run_bash(f'cap_log_file "{log.as_posix()}" 1024 100')
    assert r.returncode == 0, r.stderr
    assert log.stat().st_size < big
    lines = log.read_text(encoding="utf-8").splitlines()
    # last 100 data lines + the self-documenting truncation note
    assert lines[0] == "49900", f"kept the wrong window: first line {lines[0]!r}"
    assert lines[-2] == "49999", "the newest line must survive"
    assert any("log-cap: truncated" in ln for ln in lines), (
        "the truncation must record itself in the file — a reader who finds the "
        "log starting mid-stream should see why, not suspect corruption")


def test_rotation_preserves_inode(tmp_path):
    """The mechanism behind the live-writer case. An mv-based rotation swaps the
    inode; every fd the daemon holds would then point at an orphan."""
    log = tmp_path / "spawn.log"
    log.write_text("\n".join(str(i) for i in range(50000)) + "\n", encoding="utf-8")
    before = log.stat().st_ino
    _run_bash(f'cap_log_file "{log.as_posix()}" 1024 100')
    assert log.stat().st_ino == before, (
        "inode changed — rotation must be copytruncate, not tail-to-tmp-then-mv; "
        "a swapped inode silently orphans the daemon's open fd 1 and fd 2")


def test_live_appender_survives_rotation(tmp_path):
    """The daemon's exact shape: a process holding the log open with >> across
    the rotation. Measured pre-fix with mv: 5/40 survived."""
    log = tmp_path / "spawn.log"
    log.write_text("\n".join(str(i) for i in range(50000)) + "\n", encoding="utf-8")
    r = _run_bash(f"""
        L="{log.as_posix()}"
        ( exec 3>>"$L"; for i in $(seq 1 30); do echo "daemon-line-$i" >&3; sleep 0.02; done ) &
        W=$!
        sleep 0.1
        cap_log_file "$L" 1024 100
        wait $W 2>/dev/null
        grep -c 'daemon-line-' "$L"
    """)
    assert r.returncode == 0, r.stderr
    survived = int((r.stdout or "0").strip().splitlines()[-1])
    assert survived >= 28, (
        f"only {survived}/30 live-appender lines survived — this is the mv "
        f"failure signature (the writer is appending to an orphaned inode)")


@pytest.mark.parametrize("arg", ['""', '"/nonexistent/dir/nope.log"'])
def test_fail_open_on_bad_input(arg):
    """Fail-open by contract: this runs on the daemon spawn path, where a
    rotation error must never be able to prevent a daemon from starting."""
    assert _run_bash(f"cap_log_file {arg}").returncode == 0


# --------------------------------------------------------------------------
# 2. the wiring — the half that rots silently
# --------------------------------------------------------------------------

WIRING = [
    ("mind-api-start.sh", "_log",       "shell append site"),
    ("mind-api-start.sh", "spawn",      "daemon stdout fd — ~87% of volume"),
    ("_runtime.sh",       "rt_log_spawn", "shell append site"),
    ("_runtime.sh",       "rt_spawn",   "daemon stdout fd — second spawn path"),
]

# NOT EXHAUSTIVE, stated so the table above is not mistaken for total coverage.
# A THIRD writer exists: mind_api/bench/run.sh:55 spawns the daemon with a
# hardcoded `>> mind_api/state/spawn.log` and is deliberately left uncapped. It
# does not source _paths.sh (so cap_log_file is not in scope there), it is a
# manually-invoked benchmark harness rather than a production path, and its runs
# are short-lived — it is not a plausible source of the multi-gigabyte growth
# this goal addresses. Capping it would mean adding a `source` to a file outside
# this goal's scope. If bench runs ever become automated or long-lived, this is
# the loose end to close.


def test_every_spawn_log_writer_is_capped():
    """Both files must call cap_log_file at BOTH their append site and their
    spawn redirect. Two independent spawn paths write this one file, so capping
    only one leaves the other unbounded."""
    for fname in ("mind-api-start.sh", "_runtime.sh"):
        text = (CORE_SCRIPTS / fname).read_text(encoding="utf-8")
        assert text.count("cap_log_file") >= 2, (
            f"{fname} calls cap_log_file {text.count('cap_log_file')}x; expected "
            f">=2 (its shell append site AND its daemon spawn redirect). "
            f"Sites: {[w for w in WIRING if w[0] == fname]}")


def test_the_cap_precedes_each_spawn_redirect():
    """Order matters: capping AFTER the redirect would bound the file only on
    the next spawn, one whole daemon lifetime late."""
    # Match the two halves SEPARATELY rather than as one contiguous literal:
    # the spawn line legitimately carries other redirections between them
    # ( added `</dev/null` so the daemon inherits no caller stdin), and
    # a contiguous-substring matcher turns that into a StopIteration whose
    # traceback says nothing about what actually changed (guard-4432 — a
    # literal-token detector must fail legibly, never opaquely).
    for fname, spawn, redirect in (("mind-api-start.sh", "$py_cmd -m mind_api.src", '>> "$SPAWN_LOG"'),
                                   ("_runtime.sh", "$py_cmd -m mind_api.src", '>> "$RT_SPAWN_LOG"')):
        lines = (CORE_SCRIPTS / fname).read_text(encoding="utf-8").splitlines()
        matches = [i for i, ln in enumerate(lines) if spawn in ln and redirect in ln]
        assert matches, (
            f"{fname}: no line contains both {spawn!r} and {redirect!r}. The spawn "
            f"redirect moved or was renamed — re-point this test at it rather than "
            f"deleting the assertion.")
        spawn_i = matches[0]
        caps = [i for i, ln in enumerate(lines) if "cap_log_file" in ln and not ln.strip().startswith("#")]
        assert any(i < spawn_i for i in caps), (
            f"{fname}: no cap_log_file call before the spawn redirect at line {spawn_i + 1}")
        assert min(spawn_i - i for i in caps if i < spawn_i) < 20, (
            f"{fname}: nearest cap_log_file is far above the spawn redirect — "
            f"confirm it still guards this specific write")


def test_helper_lives_in_the_shared_sourced_file():
    """Both writers source _paths.sh (unconditionally in mind-api-start.sh,
    conditionally in _runtime.sh), which is why the helper lives there rather
    than being duplicated into each — guard-2676, no transcription."""
    text = PATHS_SH.read_text(encoding="utf-8")
    assert "cap_log_file()" in text
    assert "mv " not in text.split("cap_log_file()")[1].split("\ncase ")[0], (
        "cap_log_file must not use mv — see the module docstring")


def test_runtime_call_sites_guard_on_helper_presence():
    """_runtime.sh sources _paths.sh CONDITIONALLY (only when agent_dir is
    undefined), so its call sites must tolerate cap_log_file being absent."""
    text = (CORE_SCRIPTS / "_runtime.sh").read_text(encoding="utf-8")
    for ln in text.splitlines():
        if "cap_log_file" in ln and not ln.strip().startswith("#"):
            assert "declare -F cap_log_file" in ln, (
                f"unguarded call in _runtime.sh: {ln.strip()!r}")
