"""Regression pin for : neither daemon launcher may background a
`cd X && cmd` CONJUNCTION.

THE DEFECT. `cd DIR && cmd &` does not background `cmd` — `&` terminates the
whole AND-LIST, so bash parses it as `( cd DIR && cmd ) &` (guard-4452). That
forks an INTERMEDIATE SHELL which sits in `do_wait` for the daemon's entire
life holding the caller's file descriptors, and `disown $!` disowns THAT shell
rather than the daemon. On POSIX the wrapper is never reaped: the non-Windows
branch of `rt_force_kill_tree` / `_force_kill_tree` kills the child and returns
before it ever reads `parent_pid`.

Measured on cc-07 2026-08-20 before the fix: the live launcher left
`PID=2173901 PPID=1 STAT=S WCHAN=do_wait bash mind-api-start.sh --restart`
with the daemon at 2173902 parented to it. After the fix, a cold start left
the daemon at PPID=1 with no wrapper at all.

TWO HALVES, and the split is deliberate. The STATIC half pins the production
files, so the pin tracks the real launchers rather than a paraphrase of them.
The BEHAVIOURAL half proves the shape is what causes the leak — without it a
future reader can only take the static assertions on faith, and a grep-only
pin silently passes if someone "fixes" the text while keeping the semantics.

The behavioural half never touches the daemon: it backgrounds `sleep` in a
scratch dir, so it cannot hijack `mind_api/state`, cannot kill a live fleet
daemon, and needs no STORAGE_BACKEND pin.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv[0])

SCRIPTS = Path(__file__).resolve().parents[1]
LAUNCHERS = {
    "_runtime.sh": SCRIPTS / "_runtime.sh",
    "mind-api-start.sh": SCRIPTS / "mind-api-start.sh",
}

# The defect shape: a `cd ... &&` line whose continuation is the backgrounded
# spawn. Matches with or without the `\` line-continuation.
CONJUNCTION_SPAWN = re.compile(
    r'cd\s+"\$PROJECT_ROOT"\s*&&\s*\\?\s*\n\s*\$py_cmd\s+-m\s+mind_api\.src[^\n]*&\s*$',
    re.MULTILINE,
)


@pytest.mark.parametrize("name", sorted(LAUNCHERS))
def test_launcher_does_not_background_a_cd_conjunction(name):
    text = LAUNCHERS[name].read_text(encoding="utf-8")
    hit = CONJUNCTION_SPAWN.search(text)
    assert hit is None, (
        f"{name} backgrounds a `cd && cmd` conjunction, which forks an "
        f"intermediate do_wait shell holding the caller's fds (guard-4452). "
        f"Put `cd \"$PROJECT_ROOT\" || exit 1` on its own line.\n"
        f"Offending text: {hit.group(0) if hit else ''!r}"
    )


@pytest.mark.parametrize("name", sorted(LAUNCHERS))
def test_launcher_cds_on_its_own_line(name):
    text = LAUNCHERS[name].read_text(encoding="utf-8")
    assert 'cd "$PROJECT_ROOT" || exit 1' in text, (
        f"{name} must `cd` on its own line so `&` applies to a simple command. "
        f"Without it `$!` is the wrapper shell, not the daemon."
    )


@pytest.mark.parametrize("name", sorted(LAUNCHERS))
def test_launcher_spawn_redirects_stdin_from_devnull(name):
    """The daemon must inherit no caller stdin (guard-4527, same mechanism)."""
    text = LAUNCHERS[name].read_text(encoding="utf-8")
    spawn_lines = [
        ln for ln in text.splitlines()
        if "$py_cmd -m mind_api.src" in ln and ln.rstrip().endswith("&")
    ]
    assert spawn_lines, f"{name}: no backgrounded spawn line found — did the launcher move?"
    for ln in spawn_lines:
        assert "</dev/null" in ln, (
            f"{name} spawn line inherits the caller's stdin: {ln.strip()!r}"
        )


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process semantics")
def test_conjunction_leaks_a_wrapper_and_the_fixed_shape_does_not(tmp_path):
    """Behavioural proof that the SHAPE is the cause, not incidental.

    Backgrounds `sleep` two ways and compares the child's PPID. The unfixed
    shape parents the sleep to an intermediate shell; the fixed shape parents
    it to init (PPID 1).
    """
    def run(script_body, name):
        """Start the backgrounded child and return (child_pid, child_ppid).

        The child writes its OWN pid to a file and then `exec`s sleep, so the
        pid stays valid. Reading the pid from the child (rather than from `$!`)
        is deliberate: `$!` is precisely the value the defect corrupts, so
        using it here would beg the question.
        """
        p = tmp_path / name
        p.write_text("#!/usr/bin/env bash\n" + script_body, encoding="utf-8")
        pidfile = tmp_path / f"{name}.pid"
        subprocess.run(
            [BASH, p.as_posix(), pidfile.as_posix()], check=False, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"{name}: backgrounded child never reported its pid")
        child_pid = int(pidfile.read_text(encoding="utf-8").strip())
        # Let the launching shell exit so reparenting to init can settle.
        time.sleep(1.0)
        ppid_out = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(child_pid)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        if not ppid_out:
            pytest.fail(f"{name}: child {child_pid} exited before its PPID could be read")
        return child_pid, int(ppid_out)

    child = "'echo $$ > \"$1\"; exec sleep 20' g336-child \"$1\""
    unfixed = f'(\n    cd "$PWD" && \\\n    bash -c {child} &\n    disown $! 2>/dev/null || true\n) >/dev/null 2>&1\n'
    fixed = f'(\n    cd "$PWD" || exit 1\n    bash -c {child} </dev/null &\n    disown $! 2>/dev/null || true\n) >/dev/null 2>&1\n'

    pids = []
    try:
        u_pid, u_ppid = run(unfixed, "unfixed.sh")
        pids.append(u_pid)
        f_pid, f_ppid = run(fixed, "fixed.sh")
        pids.append(f_pid)

        assert u_ppid != 1, (
            "control failed: the unfixed shape did NOT create an intermediate "
            "shell, so this test cannot prove the fixed shape avoids one. "
            f"sleep {u_pid} already had PPID={u_ppid}"
        )
        assert f_ppid == 1, (
            "the fixed shape still forked an intermediate shell: "
            f"sleep {f_pid} has PPID={f_ppid}, expected 1"
        )
    finally:
        for pid in pids:
            subprocess.run(["kill", "-KILL", str(pid)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
