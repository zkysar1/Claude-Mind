"""postcompact-restore.sh must not block on a never-EOF stdin ().

The hook read its payload with an unbounded one-liner:

    SID=$(python3 -c "import sys,json; print(json.load(sys.stdin)...)" ...)

Its production caller (sessionstart-orchestrator.sh) always pipes a finite
payload, so the read returns there. A HAND-RUN does not: under the Bash tool
stdin can be the harness's never-EOF socket, and json.load waits for an EOF
that never comes. Measured 2026-09-22 on cc-02 (zeta): three processes wedged
46 hours, wchan pipe_read / unix_stream_data_wait, a 0-byte log, and a restore
that silently never ran. Re-measured on cc-09 before the fix: rc=124 at a 15s
external timeout, the python3 -c child in pipe_read.

Fix: the guard-664 / g-115-3661 daemon-thread + join(timeout) read, tunable via
POSTCOMPACT_RESTORE_STDIN_TIMEOUT_S. On timeout the hook says so on stderr and
exits 3, so a timeout can never be mistaken for the empty-payload exit, which
now also says what happened instead of writing 0 bytes.

Hermetic like test_postcompact_restore_body_guard.py: a tmp PROJECT_ROOT holds
VERBATIM copies of the shell under test and its dependencies; only the exec
target (postcompact-restore.py) is a stub, and reaching it is the accept signal.
The never-EOF condition is an stdin PIPE this test never writes and never
closes, so it does not depend on what stdin the harness hands the test run.

Cases:
  A  open never-EOF pipe, 2s bound     -> exits within the guard, rc=3, says why
  B  bound runner SID piped, then EOF   -> rc=0 and the exec target is reached
                                           (the bounded read still delivers)
  C  immediate EOF (the < /dev/null shape) -> rc=0 fast, a non-empty diagnostic
  D  non-JSON payload                   -> rc=0, same diagnostic, no restore
  E  never-EOF pipe, 3600s bound        -> STILL BLOCKED at the window: proves
                                           this test can see an unbounded read,
                                           and that the bound is what ends A
  F  non-numeric bound, payload piped   -> falls back to the default bound and
                                           the exec target is still reached
Plus a structural guard pinning the fix against a revert.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

AGENT = "stdinboundagent"
RUNNER_SID = "sid-runner-10615"
MARKER = "STUB-RESTORE-REACHED"

CHILD_DEADLINE_S = "2"
HANG_GUARD_S = 20.0      # far above the 2s bound, far below the 46h wedge
FAST_S = 10.0
DISCRIMINATION_WINDOW_S = 4.0

_COPY = [
    "postcompact-restore.sh",
    "_paths.sh",
    "_platform.sh",
    "_resolve_agent_from_sid.py",
    "_session_binding.py",
    "_agents.py",
    "_paths.py",
]

pytestmark = pytest.mark.skipif(
    not (os.path.isfile(BASH) or shutil.which(BASH)),
    reason="needs a resolvable bash (checked via the same _bash_helpers.BASH the tests invoke)",
)


@pytest.fixture
def fake_repo(tmp_path):
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in _COPY:
        src = CORE_SCRIPTS / name
        if src.exists():
            shutil.copy2(src, scripts / name)
    (scripts / "postcompact-restore.py").write_text(
        "print('%s')\n" % MARKER, encoding="utf-8"
    )
    agent_dir = tmp_path / _paths.AGENTS_PARENT_DIR / AGENT
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "session" / "running-session-id").write_text(
        RUNNER_SID + "\n", encoding="utf-8"
    )
    world = tmp_path / "w"
    meta = tmp_path / "m"
    world.mkdir()
    meta.mkdir()
    # resolve_binding refuses an agent dir without local-paths.conf, which
    # would make every SID look unbound (see test_postcompact_restore_body_guard).
    (agent_dir / "local-paths.conf").write_text(
        "WORLD_PATH=%s\nMETA_PATH=%s\n" % (world, meta), encoding="utf-8"
    )
    binding = agent_dir / _paths.SESSIONS_DIRNAME / RUNNER_SID
    binding.mkdir(parents=True)
    (binding / "binding.yaml").write_text(
        "session_id: %s\nagent: %s\nmode: autonomous\n"
        "started_at: '2026-09-23T12:00:00'\nstarted_by: claude-code\n" % (RUNNER_SID, AGENT),
        encoding="utf-8",
    )
    return tmp_path


def _env(deadline):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    env["POSTCOMPACT_RESTORE_STDIN_TIMEOUT_S"] = deadline
    return env


def _argv(root):
    # BASH + .as_posix(), never a bare "bash" / str(Path) (guard-580/581).
    return [BASH, (root / "core" / "scripts" / "postcompact-restore.sh").as_posix()]


def _spawn_never_eof(root, deadline):
    """stdin is a PIPE this test never writes and never closes: the never-EOF shape."""
    kw = {"start_new_session": True} if hasattr(os, "killpg") else {}
    return subprocess.Popen(
        _argv(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_env(deadline), **kw,
    )


def _reap(p):
    """Kill the whole tree, THEN close stdin so no orphaned reader is left blocked."""
    if p.poll() is None:
        if hasattr(os, "killpg"):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            p.kill()
    try:
        p.stdin.close()
    except Exception:
        pass
    # Not communicate(): it flushes stdin, and a closed stdin raises ValueError.
    # No try/except around the reads either -- a swallowed error here reads as
    # an empty stderr, which is exactly how this helper first misreported case A.
    p.wait(timeout=10)
    return p.stdout.read(), p.stderr.read()


def _wait(p, bound):
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < bound:
        time.sleep(0.1)
    return time.time() - t0


def _run_piped(root, payload):
    return subprocess.run(
        _argv(root), input=payload, capture_output=True, text=True,
        env=_env(CHILD_DEADLINE_S), timeout=HANG_GUARD_S,
    )


def test_never_eof_stdin_is_bounded_and_loud(fake_repo):
    """A: the cc-02 wedge. Must exit on its own, non-zero, and say why."""
    p = _spawn_never_eof(fake_repo, CHILD_DEADLINE_S)
    try:
        elapsed = _wait(p, HANG_GUARD_S)
        still_running = p.poll() is None
    finally:
        out, err = _reap(p)
    assert not still_running, (
        "postcompact-restore.sh still blocked after %.0fs on a never-EOF stdin -- "
        "the unbounded json.load(sys.stdin) wedge is back (g-115-10615)" % HANG_GUARD_S
    )
    assert elapsed < FAST_S, "exited, but took %.1fs; the %ss bound was not honored" % (
        elapsed, CHILD_DEADLINE_S)
    assert p.returncode == 3, (p.returncode, err)
    assert "did not reach EOF" in err, err
    assert MARKER not in out, out


def test_piped_payload_still_reaches_the_restore(fake_repo):
    """B: the production shape. The bounded read must still deliver the payload."""
    r = _run_piped(fake_repo, json.dumps({"session_id": RUNNER_SID, "source": "compact"}))
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert MARKER in r.stdout, r.stderr


def test_immediate_eof_is_fast_and_not_silent(fake_repo):
    """C: the < /dev/null shape. Completes, and no longer writes 0 bytes."""
    t0 = time.time()
    r = _run_piped(fake_repo, "")
    assert time.time() - t0 < FAST_S
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "no session_id on stdin" in r.stderr, r.stderr
    assert MARKER not in r.stdout, r.stdout


def test_non_json_payload_is_a_clean_diagnosed_skip(fake_repo):
    """D: garbage in -> no restore, rc=0, and the same diagnostic (never exit 3)."""
    r = _run_piped(fake_repo, "not json at all")
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert "no session_id on stdin" in r.stderr, r.stderr
    assert MARKER not in r.stdout, r.stdout


def test_discrimination_unbounded_read_really_blocks(fake_repo):
    """E: with the bound pushed to an hour the child must still be BLOCKED at the
    window. If it exits early, case A's exit was not the bound's doing -- the
    read never happened, or something else ended the script -- and A's green
    would prove nothing."""
    p = _spawn_never_eof(fake_repo, "3600")
    try:
        _wait(p, DISCRIMINATION_WINDOW_S)
        blocked = p.poll() is None
    finally:
        out, err = _reap(p)
    assert blocked, (
        "exited within %.0fs despite a 3600s bound (rc=%s): the never-EOF case is "
        "not exercising a blocking read. stderr: %s" % (DISCRIMINATION_WINDOW_S, p.returncode, err)
    )


def test_non_numeric_bound_falls_back_and_still_restores(fake_repo):
    """F: a bad POSTCOMPACT_RESTORE_STDIN_TIMEOUT_S must not cost the restore.
    With a bare float() the reader raised before reading, printed nothing, and a
    VALID payload was skipped under the "no session_id on stdin" message -- the
    silent-skip this goal exists to end. Falls back like hook_helpers.py does."""
    r = subprocess.run(
        _argv(fake_repo), input=json.dumps({"session_id": RUNNER_SID, "source": "compact"}),
        capture_output=True, text=True, env=_env("ten"), timeout=HANG_GUARD_S,
    )
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert MARKER in r.stdout, r.stderr
    assert "no session_id on stdin" not in r.stderr, r.stderr


def test_stdin_read_is_bounded_structurally():
    """Pin the fix: the bounded reader is present and the bare one-liner is gone."""
    src = (CORE_SCRIPTS / "postcompact-restore.sh").read_text(encoding="utf-8")
    assert "POSTCOMPACT_RESTORE_STDIN_TIMEOUT_S" in src
    assert "threading.Thread(target=_reader, daemon=True)" in src
    assert "t.join(t_s)" in src
    assert "print(json.load(sys.stdin).get('session_id',''))" not in src, (
        "postcompact-restore.sh reverted to the unbounded json.load(sys.stdin) "
        "one-liner that wedged 46h on cc-02 (g-115-10615)"
    )
