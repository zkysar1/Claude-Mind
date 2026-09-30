"""test_bash_inflight.py — in-flight Bash command windows ().

bash-agent-inject.py opens a window per command; bash-edit-record.sh closes its
own and credits each file change to the one session whose command was running
when the file changed. These tests pin the module's contract and the
injector's half of it. The recorder's half is in test_bash_edit_record.py, the
commit's half in test_iteration_commit_session_scope.py.

WHAT THESE TESTS PIN:
  1. attribute(): one running session gets the change, several make it
     ambiguous, none leaves it unattributed; a start is compared by whole
     second, as the mtime is.
  2. A window lives until its command's own timeout plus the slack, so a
     command another gate refused stops counting once it could no longer be
     running; an expired window is dropped when the session opens its next one.
  3. A half-written window counts as open (the conservative reading), a missing
     one as closed.
  4. A session dir is never created: only a bound session has one.
  5. End to end through bash-agent-inject main(): the window is keyed by the
     command text the hook EMITS, which is the text PostToolUse receives.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))

import _bash_inflight as bif  # noqa: E402

SID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def _session(tmp_path: Path, sid: str) -> Path:
    d = tmp_path / "sessions" / sid
    d.mkdir(parents=True)
    return d


def test_attribute_one_several_none():
    windows = [(SID_A, 100.0), (SID_B, 200.0)]
    assert bif.attribute(150, windows) == (SID_A, [])
    assert bif.attribute(250, windows) == ("", [SID_A, SID_B])
    assert bif.attribute(50, windows) == ("", [])
    # Several windows of ONE session are one candidate (parallel commands).
    assert bif.attribute(250, [(SID_A, 100.0), (SID_A, 200.0)]) == (SID_A, [])


def test_attribute_compares_the_start_by_whole_second():
    """mtime is whole seconds, so a command that started at 100.7 is a
    candidate for a change stamped 100 (the change may be its own at 100.9)."""
    assert bif.attribute(100, [(SID_A, 100.7)]) == (SID_A, [])
    assert bif.attribute(99, [(SID_A, 100.7)]) == ("", [])


def test_a_window_lives_for_the_commands_timeout_plus_slack(tmp_path, monkeypatch):
    monkeypatch.delenv("BASH_DEFAULT_TIMEOUT_MS", raising=False)
    monkeypatch.delenv("BASH_MAX_TIMEOUT_MS", raising=False)
    sd = _session(tmp_path, SID_A)
    bif.open_window(sd, "k-default", None, now=1000.0)
    bif.open_window(sd, "k-long", 600000, now=1000.0)
    bif.open_window(sd, "k-over-cap", 9_000_000, now=1000.0)
    w = {p.name: json.loads(p.read_text()) for p in (sd / bif.DIRNAME).iterdir()}
    assert w["k-default"] == {"start": 1000.0, "expires": 1000.0 + 120 + bif.SLACK_S}
    assert w["k-long"]["expires"] == 1000.0 + 600 + bif.SLACK_S
    assert w["k-over-cap"]["expires"] == 1000.0 + 600 + bif.SLACK_S


def test_close_returns_the_start_and_open_windows_drops_the_expired(tmp_path):
    sa, sb = _session(tmp_path, SID_A), _session(tmp_path, SID_B)
    bif.open_window(sa, "a1", None, now=1000.0)
    bif.open_window(sb, "b1", None, now=1100.0)
    assert sorted(bif.open_windows(tmp_path / "sessions", now=1150.0)) == [(SID_A, 1000.0), (SID_B, 1100.0)]
    # a1 expires at 1180: past that only b1 is still running.
    assert bif.open_windows(tmp_path / "sessions", now=1200.0) == [(SID_B, 1100.0)]
    assert bif.close_window(sb, "b1") == 1100.0
    assert bif.close_window(sb, "b1") is None, "a second close must find nothing"
    assert not (sb / bif.DIRNAME / "b1").exists()


def test_opening_a_window_drops_this_sessions_expired_ones(tmp_path):
    sd = _session(tmp_path, SID_A)
    bif.open_window(sd, "refused", None, now=1000.0)   # no PostToolUse ever closes it
    bif.open_window(sd, "next", None, now=2000.0)
    assert sorted(p.name for p in (sd / bif.DIRNAME).iterdir()) == ["next"]


def test_a_half_written_window_counts_as_open(tmp_path):
    sd = _session(tmp_path, SID_A)
    (sd / bif.DIRNAME).mkdir()
    (sd / bif.DIRNAME / "partial").write_text('{"sta')
    assert [s for s, _ in bif.open_windows(tmp_path / "sessions")] == [SID_A]


def test_a_session_dir_is_never_created(tmp_path):
    bif.open_window(tmp_path / "sessions" / SID_A, "k", None)
    assert not (tmp_path / "sessions").exists()


# --- bash-agent-inject.py, end to end through main() ------------------------

_spec = importlib.util.spec_from_file_location(
    "bash_agent_inject", CORE_SCRIPTS / "bash-agent-inject.py")
bai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bai)


def _run_main(monkeypatch, root: Path, tool_input: dict) -> dict:
    monkeypatch.setattr(bai, "resolve_binding_with_diagnostics",
                        lambda sid, root_: (SimpleNamespace(agent="alpha"), None))
    monkeypatch.setattr(bai, "_mark_binding_resolved", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bai, "_log_binding_miss_once", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bai, "_maybe_tick_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(bai, "_agent_dir", lambda _root, name: root / "agents" / name)
    payload = {"session_id": SID_A, "tool_name": "Bash", "tool_input": tool_input}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        bai.main()
    except SystemExit:
        pass
    text = out.getvalue().strip()
    return json.loads(text) if text else {}


def test_main_keys_the_window_on_the_command_it_emits(tmp_path, monkeypatch):
    sd = tmp_path / "agents" / "alpha" / "sessions" / SID_A
    sd.mkdir(parents=True)
    before = time.time()

    result = _run_main(monkeypatch, tmp_path, {"command": "python3 x.py", "timeout": 300000})

    emitted = result["hookSpecificOutput"]["updatedInput"]["command"]
    assert emitted != "python3 x.py", "the hook no longer rewrites; re-derive what PostToolUse sees"
    window = sd / bif.DIRNAME / bif.command_key(emitted)
    assert window.exists(), sorted(p.name for p in (sd / bif.DIRNAME).iterdir())
    w = json.loads(window.read_text())
    assert before <= w["start"] <= time.time()
    assert w["expires"] == w["start"] + 300 + bif.SLACK_S


def test_main_opens_no_window_for_a_session_without_a_dir(tmp_path, monkeypatch):
    result = _run_main(monkeypatch, tmp_path, {"command": "python3 x.py"})

    assert "updatedInput" in result["hookSpecificOutput"]
    assert not (tmp_path / "agents" / "alpha" / "sessions").exists()
