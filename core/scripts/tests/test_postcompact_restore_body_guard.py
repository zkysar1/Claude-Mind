"""postcompact-restore.sh runner-identity guard — BOTH axes ().

The guard at postcompact-restore.sh must admit a worker Body while still
refusing an observer. Those are the two axes guard-2319 requires: a
refusal-only suite is passed perfectly by an implementation that refuses
everything, which is exactly the bug this goal fixes (the guard refused
every non-runner, so the body-keyed checkpoint precompact wrote was never
consumed and the g-306-126 soak's worker leg ended silently at 18:38).

Production shape (guard-920): the hook receives ONE thing — a JSON object on
stdin carrying session_id — and inherits NO env vars. These tests replicate
that literally rather than the contract-ideal "call it with an agent name".

Hermetic without touching the live repo: PROJECT_ROOT in _paths.sh is derived
from the script's own location and has no env override, so the tmp tree gets a
real core/scripts/ holding VERBATIM copies of the shell under test plus its
dependencies. The exec target (postcompact-restore.py) is the one stub — it
prints a marker, so reaching it IS the accept signal. The guard itself runs
unmodified; nothing here re-implements its predicate.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _paths  # noqa: E402
from _bash_helpers import BASH  # noqa: E402

AGENT = "bodyguardagent"
RUNNER_SID = "sid-runner-0001"
BODY_SID = "sid-body-0002"
OBSERVER_SID = "sid-observer-0003"

MARKER = "STUB-RESTORE-REACHED"

# Copied verbatim from the live tree. _platform.sh is sourced by the script
# under test; _resolve_agent_from_sid.py is invoked by it.
_COPY = [
    "postcompact-restore.sh",
    "_paths.sh",
    "_platform.sh",
    "_resolve_agent_from_sid.py",
    "_session_binding.py",
    "_agents.py",
    "_paths.py",
]

# Predicate the skip on the SAME resolver the tests invoke. shutil.which("bash")
# and _bash_helpers.BASH resolve by different mechanisms (BASH also searches
# MIND_SHELL and the Git Bash candidate paths), so a which()-based skip can
# disagree with it: on a Windows box with Git Bash installed but not on PATH,
# which() returns None while BASH resolves fine, and all 7 tests SILENTLY skip.
# A skip is not a failure, so the suite reports green having run nothing — on
# the one platform whose bug (guard-580/581) these tests exist to guard.
pytestmark = pytest.mark.skipif(
    not (os.path.isfile(BASH) or shutil.which(BASH)),
    reason="needs a resolvable bash (checked via the same _bash_helpers.BASH the tests invoke)",
)


@pytest.fixture
def fake_repo(tmp_path):
    """A tmp PROJECT_ROOT whose core/scripts holds the real shell under test."""
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in _COPY:
        src = CORE_SCRIPTS / name
        if src.exists():
            shutil.copy2(src, scripts / name)

    # The ONLY stub: the exec target. Reaching it means the guard admitted us.
    (scripts / "postcompact-restore.py").write_text(
        "import os\n"
        "print('%s')\n"
        "print('MIND_SID=' + os.environ.get('MIND_SID', ''))\n"
        "print('MIND_AGENT=' + os.environ.get('MIND_AGENT', ''))\n" % MARKER,
        encoding="utf-8",
    )

    agent_dir = tmp_path / _paths.AGENTS_PARENT_DIR / AGENT
    (agent_dir / "session").mkdir(parents=True)
    (agent_dir / "session" / "running-session-id").write_text(
        RUNNER_SID + "\n", encoding="utf-8"
    )
    # REQUIRED, and not cosmetic: resolve_binding refuses an agent dir without
    # a local-paths.conf (reason `local-paths-conf-missing`), so every SID
    # resolves to "" and the hook exits at its no-agent branch — which looks
    # exactly like the guard refusing. Found by reading the resolver's
    # diagnostics rather than the swallowed `except Exception: pass`.
    world = tmp_path / "w"
    meta = tmp_path / "m"
    world.mkdir()
    meta.mkdir()
    (agent_dir / "local-paths.conf").write_text(
        "WORLD_PATH=%s\nMETA_PATH=%s\n" % (world, meta), encoding="utf-8"
    )
    return tmp_path


def _bind(root: Path, sid: str) -> Path:
    """Phase-2.6 binding so _resolve_agent_from_sid.py resolves this SID.

    `session_id` is REQUIRED and is not decorative: resolve_binding validates
    it against the SID and returns reason `session-id-mismatch` without it, so
    a binding missing this field resolves to no agent at all. Copied from the
    shape /start actually writes (guard-920) rather than the 4-field summary
    in CLAUDE.md, which omits it.
    """
    d = root / _paths.AGENTS_PARENT_DIR / AGENT / _paths.SESSIONS_DIRNAME / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "binding.yaml").write_text(
        "session_id: %s\nagent: %s\nmode: autonomous\n"
        "started_at: '2026-08-04T12:00:00'\nstarted_by: claude-code\n" % (sid, AGENT),
        encoding="utf-8",
    )
    return d


def _fork_body_wm(root: Path, sid: str) -> Path:
    """The rail body_state_path() keys on: only a worker forks a per-session WM."""
    d = _bind(root, sid)
    (d / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    return d


def _run(root: Path, sid: str):
    """Invoke the hook exactly as the harness does: JSON on stdin, no env."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("MIND_")
    }
    return subprocess.run(
        # BASH, not a bare "bash": argv[0] "bash" resolves to System32 WSL on
        # win32 and hangs past the timeout (guard-580). .as_posix(), not
        # str(Path): bash silently strips a str(WindowsPath)'s backslashes
        # (guard-581). Both caught by the pre-commit gate on this very file.
        [BASH, (root / "core" / "scripts" / "postcompact-restore.sh").as_posix()],
        input=json.dumps({"session_id": sid}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ------------------------------------------------------- axis 1: ACCEPT


def test_runner_is_admitted(fake_repo):
    """Baseline that must not regress: the reducer still restores."""
    _bind(fake_repo, RUNNER_SID)
    r = _run(fake_repo, RUNNER_SID)
    assert MARKER in r.stdout, r.stderr


def test_worker_body_is_admitted(fake_repo):
    """THE fix (): a body's SID != running-session-id, and it must
    still restore — precompact wrote a body-keyed checkpoint for it."""
    _fork_body_wm(fake_repo, BODY_SID)
    r = _run(fake_repo, BODY_SID)
    assert MARKER in r.stdout, r.stderr


def test_admitted_body_carries_its_own_sid(fake_repo):
    """The MIND_SID export is load-bearing, not decorative: without it
    body_state_path() in the .py takes the agent-wide fallback and the body
    restores the REDUCER's checkpoint."""
    _fork_body_wm(fake_repo, BODY_SID)
    r = _run(fake_repo, BODY_SID)
    assert ("MIND_SID=" + BODY_SID) in r.stdout, r.stdout
    assert ("MIND_AGENT=" + AGENT) in r.stdout, r.stdout


def test_body_admitted_even_when_no_runner_is_active(fake_repo):
    """A body whose reducer has stopped still owns a real checkpoint, and the
    .py reads only that body's own file. Deliberate: the guard is not gated on
    RUNNING_SID for bodies."""
    (fake_repo / _paths.AGENTS_PARENT_DIR / AGENT / "session"
     / "running-session-id").write_text("", encoding="utf-8")
    _fork_body_wm(fake_repo, BODY_SID)
    r = _run(fake_repo, BODY_SID)
    assert MARKER in r.stdout, r.stderr


# ------------------------------------------------------- axis 2: REFUSE


def test_observer_is_still_refused(fake_repo):
    """The 2026-05-10 bravo incident must stay fixed. An observer is bound but
    never forks a body WM, so it is NOT a body and must not receive the
    runner's resume imperative."""
    _bind(fake_repo, OBSERVER_SID)  # bound, NO working-memory.yaml
    r = _run(fake_repo, OBSERVER_SID)
    assert MARKER not in r.stdout, r.stdout
    assert r.returncode == 0, r.stderr  # refusal is a clean skip, not an error


def test_unbound_sid_is_refused(fake_repo):
    """No agent resolves for this SID -> nothing to restore."""
    r = _run(fake_repo, "sid-never-bound-9999")
    assert MARKER not in r.stdout, r.stdout
    assert r.returncode == 0, r.stderr


def test_empty_session_dir_is_not_a_body(fake_repo):
    """A per-session dir alone does not make a body — the forked WM does.
    Pins the predicate to the same rail _paths.body_state_path() uses, so the
    two cannot silently diverge."""
    d = _bind(fake_repo, OBSERVER_SID)
    assert not (d / "working-memory.yaml").exists()
    r = _run(fake_repo, OBSERVER_SID)
    assert MARKER not in r.stdout, r.stdout
