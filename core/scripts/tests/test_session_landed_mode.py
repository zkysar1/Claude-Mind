"""The LANDED predicate, and the two mode readers that report it (2026-10-07).

A worker /stop's last call rebinds its own session to reader or assistant, so the
session keeps working in that mode once its Body is closed. The rule is defined
once, `_session_binding.landed_mode_in`: a forked working-memory.yaml AND a binding
mode of reader or assistant. `session-mode-get.sh` mirrors it by hand for latency,
so the shell copy is checked against `session.py mode get` on the same fixtures,
through the REAL scripts in a tmp project root. Why the session lands:
core/config/rationale/worker-stop-close.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

from _bash_helpers import BASH  # noqa: E402
from _session_binding import is_landed_mode, landed_mode, landed_mode_in  # noqa: E402

AGENT = "landagent"
SID = "33333333-3333-4333-8333-333333333333"

# What session.py's `_paths` import needs in a tmp root (the same set the
# post-compaction banner harness copies), plus the two readers under test.
_COPY = ["session-mode-get.sh", "session.py", "_paths.sh", "_platform.sh",
         "_paths.py", "_path_helpers.py", "_resolve_agent_from_sid.py",
         "_session_binding.py", "_agents.py"]


def _session(root: Path, *, fork: bool = True, mode: str | None = "assistant",
             raw: str | None = None, sid: str = SID) -> Path:
    d = root / "agents" / AGENT / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    if fork:
        (d / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    if raw is None and mode is not None:
        raw = (f"session_id: {sid}\nagent: {AGENT}\nmode: {mode}\n"
               "started_at: '2026-10-07T13:00:00'\nstarted_by: worker-stop\n")
    if raw is not None:
        (d / "binding.yaml").write_text(raw, encoding="utf-8")
    return d


# ───────────────────────────── the predicate ─────────────────────────────

@pytest.mark.parametrize("mode,expected", [
    ("assistant", "assistant"), ("reader", "reader"), ("autonomous", None)])
def test_landed_needs_a_reader_or_assistant_binding(tmp_path, mode, expected):
    assert landed_mode_in(_session(tmp_path, mode=mode)) == expected


def test_landed_needs_the_fork(tmp_path):
    """A reader or assistant binding without a fork is an ordinary observer
    session. Only a worker forks, so only a worker's /stop can land a session."""
    assert landed_mode_in(_session(tmp_path, fork=False)) is None


def test_a_missing_binding_is_not_landed(tmp_path):
    assert landed_mode_in(_session(tmp_path, mode=None)) is None


@pytest.mark.parametrize("raw", [
    "mode: 'assistant'\n", 'mode: "reader"\n', "mode:   assistant   # landed\n"])
def test_quoted_or_commented_mode_still_reads(tmp_path, raw):
    assert landed_mode_in(_session(tmp_path, raw=raw)) in ("assistant", "reader")


@pytest.mark.parametrize("raw", [
    "", "agent: x\n", "mode:\n", "mode: assistants\n", "mode: Assistant\n",
    "  mode: assistant\n"])
def test_anything_else_is_not_landed(tmp_path, raw):
    assert landed_mode_in(_session(tmp_path, raw=raw)) is None


def test_landed_mode_validates_sid_and_agent(tmp_path):
    _session(tmp_path)
    assert landed_mode(tmp_path, AGENT, SID) == "assistant"
    assert landed_mode(tmp_path, AGENT, "../" + SID) is None
    assert landed_mode(tmp_path, AGENT, "") is None
    assert landed_mode(tmp_path, "../x", SID) is None


def test_is_landed_mode():
    assert is_landed_mode("assistant") and is_landed_mode(" reader ")
    assert not any(is_landed_mode(m) for m in ("autonomous", "", None))


# ──────────────── the two readers, through the real scripts ────────────────

pytestmark = pytest.mark.skipif(
    not (os.path.isfile(BASH) or shutil.which(BASH)),
    reason="needs a resolvable bash (checked via the same _bash_helpers.BASH the tests invoke)",
)


@pytest.fixture
def repo(tmp_path):
    scripts = tmp_path / "core" / "scripts"
    scripts.mkdir(parents=True)
    for name in _COPY:
        shutil.copy2(CORE_SCRIPTS / name, scripts / name)
    agent_dir = tmp_path / "agents" / AGENT
    (agent_dir / "session").mkdir(parents=True)
    (tmp_path / "w").mkdir()
    (tmp_path / "m").mkdir()
    (agent_dir / "local-paths.conf").write_text(
        f"WORLD_PATH={(tmp_path / 'w').as_posix()}\nMETA_PATH={(tmp_path / 'm').as_posix()}\n",
        encoding="utf-8")
    # The box's agent-wide mode: what an unlanded session reports.
    (agent_dir / "session" / "agent-mode").write_text("autonomous\n", encoding="utf-8")
    return tmp_path


def _modes(root: Path, sid: str = SID) -> tuple:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MIND_", "BODY_"))}
    env.update(MIND_AGENT=AGENT, MIND_SID=sid, STORAGE_BACKEND="local")
    scripts = root / "core" / "scripts"
    sh = subprocess.run([BASH, (scripts / "session-mode-get.sh").as_posix()],
                        env=env, capture_output=True, text=True, timeout=30)
    py = subprocess.run([sys.executable, (scripts / "session.py").as_posix(), "mode", "get"],
                        env=env, capture_output=True, text=True, timeout=30)
    assert sh.returncode == 0 and py.returncode == 0, (sh.stderr, py.stderr)
    return sh.stdout.strip(), py.stdout.strip()


@pytest.mark.parametrize("setup,sid,expected", [
    ({"mode": "assistant"}, SID, "assistant"),
    ({"mode": "reader"}, SID, "reader"),
    ({"raw": "mode: 'assistant'\n"}, SID, "assistant"),
    ({"mode": "autonomous"}, SID, "autonomous"),        # a live worker: the box's mode
    ({"mode": "assistant", "fork": False}, SID, "autonomous"),  # an observer
    ({"mode": None}, SID, "autonomous"),                # no binding at all
    ({"mode": "assistant"}, "", "autonomous"),          # guard-6178: empty SID picks nothing
    ({"mode": "assistant"}, "x/../" + SID, "autonomous"),
])
def test_shell_and_python_readers_agree(repo, setup, sid, expected):
    _session(repo, **setup)
    sh, py = _modes(repo, sid)
    assert (sh, py) == (expected, expected), (
        f"session-mode-get.sh={sh!r} session.py={py!r}, expected {expected!r}")


def test_no_session_dir_reads_the_agent_wide_file(repo):
    assert _modes(repo) == ("autonomous", "autonomous")
    (repo / "agents" / AGENT / "session" / "agent-mode").unlink()
    assert _modes(repo) == ("reader", "reader")         # DEFAULT_MODE, both sides
