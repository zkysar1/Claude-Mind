"""Tests for stop-signal-relay.py — the tool-call-boundary stop relay ().

The hook exists because the loop's SOLE consumption point for `stop-requested`
is Phase -1.4, the top of an iteration, while a goal execution spans
arbitrarily many tool calls inside ONE iteration. These tests pin the predicate
that decides whether the signal is relayed, because every branch of it is a
decision to stay SILENT while a shutdown grace burns.

The fixtures build a throwaway PROJECT_ROOT and inject a stub `_paths` module,
so no test ever creates a real `agents/<agent>/session/stop-requested` — the
agent is forbidden to write that file, and a test that did would be
indistinguishable from a live stop.
"""

import importlib.util
import json
import pathlib
import sys
import types

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

SID = "11111111-2222-3333-4444-555555555555"
AGENT = "fixtureagent"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "stop_signal_relay_under_test", SCRIPTS / "stop-signal-relay.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_root(tmp_path, *, stop_requested=True, state="RUNNING",
               stop_loop=False, runner=SID, binding=True):
    root = tmp_path / "proj"
    sess = root / "agents" / AGENT / "session"
    sess.mkdir(parents=True)
    if binding:
        bdir = root / "agents" / AGENT / "sessions" / SID
        bdir.mkdir(parents=True)
        (bdir / "binding.yaml").write_text("agent: %s\n" % AGENT, encoding="utf-8")
    if stop_requested:
        (sess / "stop-requested").write_text("", encoding="utf-8")
    if stop_loop:
        (sess / "stop-loop").write_text("", encoding="utf-8")
    (sess / "agent-state").write_text(state + "\n", encoding="utf-8")
    if runner is not None:
        (sess / "running-session-id").write_text(runner + "\n", encoding="utf-8")
    return root


def _run(mod, root, monkeypatch, capsys, payload=None, raw=None):
    """Drive main() with a stub `_paths` and a canned stdin payload."""
    stub = types.ModuleType("_paths")
    stub.PROJECT_ROOT = str(root)
    monkeypatch.setitem(sys.modules, "_paths", stub)
    if raw is None:
        raw = json.dumps(payload if payload is not None
                         else {"tool_name": "Bash", "session_id": SID})
    monkeypatch.setattr(mod, "_read_stdin_with_timeout", lambda *a, **k: raw)
    rc = mod.main()
    return rc, capsys.readouterr().out


def test_relays_when_stop_is_pending_and_unconsumed(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path), monkeypatch, capsys)
    assert rc == 0
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    ctx = hso["additionalContext"]
    # The imperative must name the exact next tool call. A relay that merely
    # says "a stop is pending" leaves the reader to rediscover the entry point
    # while the grace burns.
    assert "Skill(aspirations-graceful-stop)" in ctx
    assert "Phase -1.4" in ctx
    assert AGENT in ctx


def test_silent_when_no_stop_requested(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path, stop_requested=False), monkeypatch, capsys)
    assert rc == 0
    assert out == ""


def test_silent_once_stop_loop_is_set(tmp_path, monkeypatch, capsys):
    """stop-loop present => /stop reached D2; the sequence is already running."""
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path, stop_loop=True), monkeypatch, capsys)
    assert rc == 0
    assert out == ""


def test_silent_when_agent_is_not_running(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path, state="IDLE"), monkeypatch, capsys)
    assert rc == 0
    assert out == ""


def test_silent_for_a_session_that_does_not_own_the_loop(tmp_path, monkeypatch, capsys):
    """An observer session must not be pushed into a stop it does not own."""
    mod = _load_module()
    root = _make_root(tmp_path, runner="99999999-0000-0000-0000-000000000000")
    rc, out = _run(mod, root, monkeypatch, capsys)
    assert rc == 0
    assert out == ""


def test_relays_when_running_session_id_is_absent(tmp_path, monkeypatch, capsys):
    """Only a DEFINITE mismatch silences the relay (runner-identity-check shape).

    A vessel mid-execution needs the signal more than the relay needs certainty,
    so an absent/unreadable running-session-id still relays.
    """
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path, runner=None), monkeypatch, capsys)
    assert rc == 0
    assert "Skill(aspirations-graceful-stop)" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_silent_when_the_agent_cannot_be_resolved(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path, binding=False), monkeypatch, capsys)
    assert rc == 0
    assert out == ""


def test_silent_on_malformed_or_empty_stdin(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    root = _make_root(tmp_path)
    rc, out = _run(mod, root, monkeypatch, capsys, raw="not json at all")
    assert rc == 0 and out == ""
    rc, out = _run(mod, root, monkeypatch, capsys, raw="")
    assert rc == 0 and out == ""


def test_traversal_shaped_session_id_is_refused(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    root = _make_root(tmp_path)
    rc, out = _run(mod, root, monkeypatch, capsys,
                   payload={"tool_name": "Bash", "session_id": "../../etc"})
    assert rc == 0
    assert out == ""


def test_emitted_payload_is_a_single_json_object(tmp_path, monkeypatch, capsys):
    """Claude Code parses hook stdout as one JSON document; extra output breaks it."""
    mod = _load_module()
    rc, out = _run(mod, _make_root(tmp_path), monkeypatch, capsys)
    assert rc == 0
    json.loads(out)  # raises if anything else was printed alongside
    assert out.count("\n") == 1
