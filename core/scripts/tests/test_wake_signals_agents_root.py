"""_wake_signals.touch_peer_signals writes under the agents root it is given (2026-10-07).

A daemon endpoint passes its request's ctx.paths.agents_root and agent_name,
because this module's own location and the process's MIND_AGENT name neither
the request's tree nor its agent. The endpoint call sites are pinned by
mind_api/tests/test_runtime_board_write.py and
mind_api/tests/test_wrapper_aspirations_retire_release_claim.py. Every test here
writes under tmp_path only.
"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _wake_signals as ws  # noqa: E402


def _agents(root: Path, *names: str) -> Path:
    for name in names:
        (root / name / "session").mkdir(parents=True)
    return root


def test_peers_under_the_given_root_are_woken_and_the_given_self_is_not(tmp_path, monkeypatch):
    """The request's agent is self, whatever this process is bound to."""
    monkeypatch.setenv("MIND_AGENT", "envagent")
    root = _agents(tmp_path / "agents", "alpha", "bravo", "envagent")
    assert ws.touch_peer_signals("board-activity", agents_root=root, self_agent="alpha") == 2
    assert (root / "bravo" / "session" / "board-activity").exists()
    assert (root / "envagent" / "session" / "board-activity").exists()
    assert not (root / "alpha" / "session" / "board-activity").exists()


def test_without_arguments_the_cli_behaviour_is_unchanged(tmp_path, monkeypatch):
    """CLI callers pass nothing: the module root and MIND_AGENT. The module root
    is pointed at tmp, so the real repo is never touched, and the opt-in lets
    the touch run under pytest."""
    root = _agents(tmp_path / "agents", "alpha", "bravo")
    monkeypatch.setattr(ws, "_AGENTS_ROOT", root)
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("WAKE_SIGNALS_ALLOW_PYTEST", "1")
    assert ws.touch_peer_signals("goal-claim-released") == 1
    assert (root / "bravo" / "session" / "goal-claim-released").exists()
    assert not (root / "alpha" / "session" / "goal-claim-released").exists()


def test_under_pytest_the_module_root_is_never_touched(tmp_path, monkeypatch):
    """A CLI caller under pytest (board.py run by a test) cannot redirect the
    module root, so the touch is refused. The root here is tmp, so this test
    writes nothing real even when the refusal is broken."""
    root = _agents(tmp_path / "agents", "alpha", "bravo")
    monkeypatch.setattr(ws, "_AGENTS_ROOT", root)
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.delenv("WAKE_SIGNALS_ALLOW_PYTEST", raising=False)
    assert ws.touch_peer_signals("board-activity") == 0
    assert not (root / "bravo" / "session" / "board-activity").exists()
