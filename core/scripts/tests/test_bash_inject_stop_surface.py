""": the PreToolUse[Bash] hook surfaces a PENDING STOP the loop has not
yet reached, and does it WITHOUT disturbing the injection the hook exists for.

WHY THIS LIVES IN THE HOOK AT ALL. `stop-requested` is read at exactly ONE place
in the loop -- aspirations Phase -1.4 -- so detection is keyed to loop STRUCTURE.
Measured on a live vessel 2026-09-13 (instance i-022f74084032d8c56, reserve
350s): the sidecar raised the signal at 16:49:25 and the mind first probed it at
16:54:49, 324s later, with 26s of grace left; consolidation never ran and
handoff.yaml was never written. Of that latency 213s was /start still finishing
and 111s the loop's own preamble, so reordering /aspirations recovers at most a
third. This hook fires on EVERY Bash call, so detection here is one tool call.

THE TWO HALVES ARE TESTED SEPARATELY AND BOTH ARE REQUIRED:
  - the PREDICATE (_maybe_surface_stop): when does it speak, and when is it
    silent. Hermetic, tmp_path only -- it must never read the real agent dir.
  - the PAYLOAD WIRING (main): the advisory must ride the fields that actually
    reach the model AND must not cost the injection. A correct predicate wired
    into a field the model never sees is the failure this file exists to catch
    (guard-5501: a diagnostic's silence is not evidence until you prove it can
    fire), and `allow` + permissionDecisionReason ALONE was measured NOT to
    deliver (g-115-3511).

THE SILENT CASES ARE THE POINT, not filler: this hook runs before every Bash
call on every box in the fleet, so a predicate that fires when it should not is
worse than one that never fires at all.

NOTE the tests here never create a real `agents/<agent>/session/stop-requested`
-- Claude must not write that signal (user-interaction.md, Script-Level
Restrictions), so the payload half injects the advisory instead of provoking it.

Run: STORAGE_BACKEND=local py -3 -m pytest \
    core/scripts/tests/test_bash_inject_stop_surface.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "bash_agent_inject_stop_surface", CORE_SCRIPTS / "bash-agent-inject.py")
bai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bai)

AGENT = "agent-under-test"


def _session_dir(root: Path) -> Path:
    d = root / "agents" / AGENT / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _expire_throttle(root: Path, sid: str) -> None:
    """Age the stamp past the interval so the next call is due again."""
    stamp = root / "core" / "logs" / "stop-surface-hook" / sid
    old = time.time() - (bai.STOP_SURFACE_INTERVAL_S + 60)
    os.utime(stamp, (old, old))


# --- the predicate: when it speaks -----------------------------------------

def test_pending_stop_is_surfaced_and_names_the_signal_age_and_obligation(tmp_path):
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    msg = bai._maybe_surface_stop(AGENT, "sid-a", tmp_path)

    assert msg, "a pending, unhonored stop must be surfaced"
    assert "stop-requested" in msg, "the message must name the signal"
    assert "ago" in msg, "the message must carry the signal's age"
    assert "-1.4" in msg, "the message must name the phase that owns the obligation"
    assert "stop-loop" in msg, (
        "the message must say not to clear the signal -- only /stop and Phase "
        "-1.4 may write stop-loop (stop-hook-compliance.md rule 2)")


def test_the_route_names_the_handler_and_says_it_works_outside_the_loop(tmp_path):
    """ R3. "Go to Phase -1.4" was the whole instruction, and a mind
    still inside /start cannot reach Phase -1.4. Measured on dev vessel
    i-058c0073c76d79e18: the mind read it on /start's last page, declined to
    boot, and ended the turn with no consolidation and no handoff. The phase does
    one thing with the signal -- it invokes the handler -- so the handler is what
    the message must name, together with the fact that it needs no loop."""
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    msg = bai._maybe_surface_stop(AGENT, "sid-route", tmp_path)

    assert "Skill(aspirations-graceful-stop)" in msg, (
        "the executable route must be named, not only the phase that calls it")
    assert "/start" in msg and "/boot" in msg, (
        "the message must say the route works from inside /start and /boot")
    assert "go to Phase -1.4" not in msg, (
        "the unreachable instruction is exactly what stranded the measured vessel")


# --- the predicate: when it is SILENT (the load-bearing half) ---------------

def test_no_stop_requested_is_silent(tmp_path):
    """THE CONTROL. This hook runs before every Bash call on every box, so a
    predicate that speaks with no stop pending is worse than one that never
    speaks at all."""
    _session_dir(tmp_path)
    assert bai._maybe_surface_stop(AGENT, "sid-b", tmp_path) == ""


def test_stop_loop_present_means_discharged_and_is_silent(tmp_path):
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")
    (sd / "stop-loop").write_text("1", encoding="utf-8")

    assert bai._maybe_surface_stop(AGENT, "sid-c", tmp_path) == "", (
        "stop-loop means the exit is already sanctioned; re-announcing it is noise")


def test_second_call_inside_the_interval_is_throttled(tmp_path):
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    assert bai._maybe_surface_stop(AGENT, "sid-d", tmp_path), "first call speaks"
    assert bai._maybe_surface_stop(AGENT, "sid-d", tmp_path) == "", (
        "a hook on every Bash call must bound its own noise")

    _expire_throttle(tmp_path, "sid-d")
    assert bai._maybe_surface_stop(AGENT, "sid-d", tmp_path), (
        "and it must RE-surface once the interval passes -- an unhonored stop "
        "is an unmet obligation, not a one-time notice")


def test_a_worker_body_is_silent_while_the_same_stop_speaks_to_its_reducer(tmp_path):
    """The agent-level signal belongs to the reducer. A worker Body must never be
    told to run the graceful stop -- its own stop is sessions/<sid>/stop-requested,
    read at worker-loop Phase -0-stop. The positive control uses the SAME signal
    file, so the silence is the role and not a missing fixture (guard-4166)."""
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    assert bai._maybe_surface_stop(AGENT, "sid-w", tmp_path, worker_body=True) == ""
    assert bai._maybe_surface_stop(AGENT, "sid-r", tmp_path, worker_body=False), (
        "control: the same pending stop must still reach a non-worker session")


def test_reader_and_assistant_sessions_are_silent(tmp_path):
    """An observer (the RUNNING-branch reader/assistant session) and an assistant
    session hold no loop whose stop this is, and the handler refuses below
    autonomous. Telling either to invoke it would misroute the runner's ending."""
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    for mode in ("reader", "assistant", " assistant\n"):
        assert bai._maybe_surface_stop(
            AGENT, "sid-obs", tmp_path, binding_mode=mode) == "", mode
    assert bai._maybe_surface_stop(AGENT, "sid-auto", tmp_path, binding_mode="autonomous"), (
        "control: an autonomous binding owns the stop and must hear the route")


def test_an_unbound_session_still_hears_the_route(tmp_path):
    """THE POPULATION THIS ROUTE EXISTS FOR (rb-9476). On the measured vessel the
    drive session's SID had NO binding.yaml, so binding_mode arrives None there.
    A predicate that required a binding would be correct-looking and inert on the
    one deployment that needed it."""
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    msg = bai._maybe_surface_stop(AGENT, "sid-unbound", tmp_path, binding_mode=None)
    assert "Skill(aspirations-graceful-stop)" in msg


def test_a_silent_session_does_not_consume_the_throttle(tmp_path):
    """A silenced call must not stamp: the stamp is per-SID, and a role that is
    told nothing has nothing to rate-limit. Stamping would also make the silence
    look like a throttle to anyone reading core/logs."""
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    assert bai._maybe_surface_stop(AGENT, "sid-s", tmp_path, worker_body=True) == ""
    assert not (tmp_path / "core" / "logs" / "stop-surface-hook" / "sid-s").exists()


def test_missing_agent_is_silent(tmp_path):
    """The hook resolves the agent before calling this; an unresolved binding
    must not raise and must not guess an agent dir."""
    assert bai._maybe_surface_stop("", "sid-e", tmp_path) == ""


def test_a_path_bearing_sid_cannot_escape_the_stamp_directory(tmp_path):
    sd = _session_dir(tmp_path)
    (sd / "stop-requested").write_text("1", encoding="utf-8")

    assert bai._maybe_surface_stop(AGENT, "../../etc/passwd", tmp_path), (
        "an unsafe sid must degrade, not refuse")
    stamps = tmp_path / "core" / "logs" / "stop-surface-hook"
    assert [p.name for p in stamps.iterdir()] == ["nosid"], (
        "a sid carrying path separators must be replaced, never joined")


# --- the payload wiring: it must REACH the model, and cost nothing ---------

def _run_hook(monkeypatch, advisory: str, binding=None, calls=None) -> dict:
    """Drive main() with a resolved binding; return the parsed stdout JSON.
    `calls`, when a list, records the keyword arguments main() hands the
    predicate -- the role facts are only wired if main() actually passes them."""
    binding = binding if binding is not None else SimpleNamespace(agent=AGENT)

    def _fake_surface(a, s, p, **kw):
        if calls is not None:
            calls.append(kw)
        return advisory

    monkeypatch.setattr(
        bai, "resolve_binding_with_diagnostics", lambda sid, root: (binding, None))
    monkeypatch.setattr(bai, "_maybe_tick_heartbeat", lambda a, s, p: None)
    monkeypatch.setattr(bai, "_maybe_surface_stop", _fake_surface)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "test-sid-stop-surface",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi", "description": "d", "timeout": 5000},
    })))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        bai.main()
    except SystemExit:
        pass
    lines = out.getvalue().strip().splitlines()
    assert lines, "the hook must always emit a payload"
    return json.loads(lines[-1])


def test_advisory_rides_every_field_that_reaches_the_model(monkeypatch):
    msg = "[stop-pending] marker"
    hs = _run_hook(monkeypatch, msg)["hookSpecificOutput"]

    assert hs["permissionDecisionReason"] == msg
    assert hs["additionalContext"] == msg


def test_advisory_is_also_on_systemMessage(monkeypatch):
    """`allow` + permissionDecisionReason ALONE was probed and did NOT deliver
    (g-115-3511, five-probe table in trailing-echo-exit-gate.py). Dropping this
    field would leave the predicate correct and the advisory unseen."""
    msg = "[stop-pending] marker"
    assert _run_hook(monkeypatch, msg)["systemMessage"] == msg


def test_advisory_never_denies_and_never_costs_the_injection(monkeypatch):
    """A deny here would wedge the very consolidation the stop is asking for --
    the mind needs tool calls to consolidate and hand off. And the injection is
    what this hook exists for; an advisory that drops it trades a 324s detection
    gap for an unbound agent on every call."""
    doc = _run_hook(monkeypatch, "[stop-pending] marker")
    hs = doc["hookSpecificOutput"]

    assert hs["permissionDecision"] == "allow"
    assert f"MIND_AGENT={AGENT}" in hs["updatedInput"]["command"]
    assert hs["updatedInput"]["command"].endswith("echo hi")
    assert hs["updatedInput"]["description"] == "d", "sibling tool_input keys survive"
    assert hs["updatedInput"]["timeout"] == 5000


def test_main_hands_the_predicate_the_bindings_mode(monkeypatch):
    """The role facts are wired only if main() actually passes them (guard-6374).
    A predicate that reads a mode main() never supplies is green in its own tests
    and still speaks to every observer session in production. The control is the
    modeless binding: the kwarg must track the binding, not be a constant."""
    calls = []
    _run_hook(monkeypatch, "", binding=SimpleNamespace(agent=AGENT, mode="reader"),
              calls=calls)
    assert calls and calls[-1].get("binding_mode") == "reader"
    assert calls[-1].get("worker_body") is False

    _run_hook(monkeypatch, "", calls=calls)
    assert calls[-1].get("binding_mode") is None, (
        "control: a binding with no mode must arrive as None, the unbound route")


def test_main_marks_a_forked_wm_session_as_a_worker_body(monkeypatch, tmp_path):
    """The worker fact is the SAME forked-WM stat that exports BODY_ROLE=worker,
    so the advisory and the store rails can never disagree about what a Body is.
    The BODY_ROLE assertion is the control that the stat really classified it."""
    monkeypatch.setattr(bai, "_agent_dir", lambda root, name: tmp_path / "agents" / name)
    fork = (tmp_path / "agents" / AGENT / bai._SESSIONS_DIRNAME
            / "test-sid-stop-surface" / "working-memory.yaml")
    fork.parent.mkdir(parents=True)
    fork.write_text("{}", encoding="utf-8")

    calls = []
    doc = _run_hook(monkeypatch, "", calls=calls)

    assert calls and calls[-1].get("worker_body") is True
    assert "BODY_ROLE=worker" in doc["hookSpecificOutput"]["updatedInput"]["command"]


def test_without_an_advisory_the_payload_is_byte_for_byte_the_old_shape(monkeypatch):
    """THE CONTROL for the wiring half: on the overwhelmingly common path
    (no stop pending) the hook must emit exactly what it emitted before."""
    doc = _run_hook(monkeypatch, "")
    hs = doc["hookSpecificOutput"]

    assert "systemMessage" not in doc
    assert "permissionDecisionReason" not in hs
    assert "additionalContext" not in hs
    assert set(hs) == {"hookEventName", "permissionDecision", "updatedInput"}
    assert f"MIND_AGENT={AGENT}" in hs["updatedInput"]["command"]
