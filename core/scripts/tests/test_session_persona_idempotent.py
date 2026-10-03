"""persona-active's mtime is the time of its last VALUE change ().

A served agent's sidecar decides "has the ceremony's turn ended?" by comparing the current
session document's save time with the newest persona-active mtime: /start writes persona
mid-turn and the document is saved at turn end, so a document at least as new as the persona
write means the turn is over. A SECOND session's /start (a conversation opened beside an idle
assistant agent) re-asserts persona `true`, and `persona set` rewrote the file every time
(temp + replace), which moved the mtime past the first session's document and made the agent
read as un-settled until that session next saved. The fix is at the writer: a set that changes
nothing writes nothing, so the mtime keeps meaning "persona last CHANGED".

Every test flips exactly one input against a control (guard-4166): the same-value cases and the
changed-value cases are pinned in this file together, so a writer that always writes and one
that never writes cannot both pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SESSION_PY = PROJECT_ROOT / "core" / "scripts" / "session.py"

# 2020-09-13: far from now, so "moved" is unmistakable and no clock granularity can hide it.
OLD_NS = 1_600_000_000 * 1_000_000_000


def _set(agent_dir: Path, value: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("MIND_SID", "MIND_AGENT_DIR")}
    env.update({
        "MIND_AGENT": "persona-probe",
        "MIND_AGENT_DIR": str(agent_dir),
        "STORAGE_BACKEND": "local",
    })
    return subprocess.run(
        [sys.executable, str(SESSION_PY), "persona", "set", value],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "persona-probe"
    (d / "session").mkdir(parents=True)
    return d


def _persona(agent_dir: Path) -> Path:
    return agent_dir / "session" / "persona-active"


def _seed(agent_dir: Path, content: str) -> None:
    p = _persona(agent_dir)
    p.write_text(content, encoding="utf-8")
    os.utime(p, ns=(OLD_NS, OLD_NS))


def _mtime_ns(agent_dir: Path) -> int:
    return _persona(agent_dir).stat().st_mtime_ns


@pytest.mark.parametrize("value", ["true", "false"])
def test_a_set_that_changes_nothing_does_not_move_the_mtime(agent_dir: Path, value: str) -> None:
    _seed(agent_dir, value + "\n")
    r = _set(agent_dir, value)
    assert r.returncode == 0, r.stderr
    assert _mtime_ns(agent_dir) == OLD_NS
    assert _persona(agent_dir).read_text(encoding="utf-8").strip() == value


@pytest.mark.parametrize("old,new", [("false", "true"), ("true", "false")])
def test_a_set_that_changes_the_value_moves_the_mtime(agent_dir: Path, old: str, new: str) -> None:
    _seed(agent_dir, old + "\n")
    r = _set(agent_dir, new)
    assert r.returncode == 0, r.stderr
    assert _mtime_ns(agent_dir) > OLD_NS
    assert _persona(agent_dir).read_text(encoding="utf-8").strip() == new


def test_the_first_set_creates_the_file(agent_dir: Path) -> None:
    assert not _persona(agent_dir).exists()
    r = _set(agent_dir, "true")
    assert r.returncode == 0, r.stderr
    assert _persona(agent_dir).read_text(encoding="utf-8").strip() == "true"


def test_equal_content_written_without_a_trailing_newline_is_not_rewritten(agent_dir: Path) -> None:
    # A bash `printf true` (no newline) holds the same value as the setter's own `true\n`.
    _seed(agent_dir, "true")
    r = _set(agent_dir, "true")
    assert r.returncode == 0, r.stderr
    assert _mtime_ns(agent_dir) == OLD_NS


def test_an_invalid_value_is_refused_and_writes_nothing(agent_dir: Path) -> None:
    _seed(agent_dir, "true\n")
    r = _set(agent_dir, "maybe")
    assert r.returncode == 1
    assert "Invalid persona value" in r.stderr
    assert _mtime_ns(agent_dir) == OLD_NS
    assert _persona(agent_dir).read_text(encoding="utf-8").strip() == "true"
