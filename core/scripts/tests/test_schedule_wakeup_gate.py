#!/usr/bin/env python3
"""Regression tests for core/scripts/schedule-wakeup-gate.py.

Two independent failure classes are covered:

A. SLASH-PREFIX prompts (2026-05-18) -- the gate's original purpose. This half
   shipped untested; these tests pin it.
B. `stop: true` cancelling the deadman net on a LIVE loop (2026-08-25). The
   deadman is a SINGLE replace-slot wakeup, so cancelling it while the agent is
   RUNNING converts a recoverable pause into a hard stop that needs a human.
C. Arming the sentinel with NO loop under it (2026-09-21). A net outlives the
   loop it was armed for, and the turn that receives its firing re-arms BEFORE
   it reads the state gate that refuses -- so an IDLE agent re-enters the same
   turn forever. Exact mirror of B: B refuses a cancel while RUNNING, C refuses
   an arm while not.

The gate is invoked exactly as production invokes it -- a subprocess reading the
PreToolUse payload from stdin (probe-with-canonical-code-path: canonical BINARY
is not canonical INVOCATION). Agent state is isolated via MIND_AGENT_DIR, the
test override `_paths.AGENT_DIR` already honors; no real agent dir is touched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "schedule-wakeup-gate.py"


def run_gate(tool_input, agent_dir=None, tool_name="ScheduleWakeup",
             session_id=None):
    """Invoke the gate as production does. Returns (rc, decision_or_None)."""
    env = dict(os.environ)
    env.pop("MIND_AGENT", None)
    if agent_dir is not None:
        env["MIND_AGENT_DIR"] = str(agent_dir)
    else:
        env.pop("MIND_AGENT_DIR", None)
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if session_id is not None:
        payload["session_id"] = session_id
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=30,
    )
    decision = None
    if proc.stdout.strip():
        try:
            decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
        except Exception:
            decision = None
    return proc.returncode, decision, proc.stdout


@pytest.fixture
def agent(tmp_path):
    """A tmp agent dir with a writable session/ subdir."""
    d = tmp_path / "zeta-test"
    (d / "session").mkdir(parents=True)
    return d


def set_state(agent, value):
    (agent / "session" / "agent-state").write_text(value, encoding="utf-8")


# ---------------------------------------------------------------- class B ---
# Outcome table from _cancel_would_strand_loop's docstring (guard-3328): every
# branch of the new discriminating test gets a case, not just the deny.

def test_stop_on_running_loop_is_denied(agent):
    """POSITIVE CONTROL. This is the exact call that stalled the loop.

    If the `stop: true` guard is removed from the gate, THIS test fails and the
    others in class B still pass -- so it is the one that proves the guard is
    live rather than merely present.
    """
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate({"stop": True}, agent_dir=agent)
    assert rc == 0
    assert decision == "deny", "stop:true on a RUNNING loop must be refused"


def test_stop_reason_names_the_recovery_action(agent):
    """A deny the model cannot act on is a deny it will retry (guard-1680:
    stderr cannot reach the model, so the structured reason is the only channel)."""
    set_state(agent, "RUNNING")
    _, _, stdout = run_gate({"stop": True}, agent_dir=agent)
    reason = json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "<<autonomous-loop-dynamic>>" in reason
    assert "stop-requested" in reason


def test_stop_allowed_when_stop_requested_is_set(agent):
    """Outcome 4: a genuine /stop writes stop-requested FIRST, so the cancel is
    legitimate. Without this the gate would break the real stop path."""
    set_state(agent, "RUNNING")
    (agent / "session" / "stop-requested").write_text("", encoding="utf-8")
    rc, decision, _ = run_gate({"stop": True}, agent_dir=agent)
    assert (rc, decision) == (0, None), "a real /stop must not be blocked"


def test_stop_allowed_when_idle(agent):
    """Outcome 3: no live loop to strand."""
    set_state(agent, "IDLE")
    rc, decision, _ = run_gate({"stop": True}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_stop_allowed_when_agent_state_missing(agent):
    """Outcome 2: fail-open. The docstring's contract is that a broken gate
    approves -- a fail-closed gate would stall loops, which is the disease."""
    rc, decision, _ = run_gate({"stop": True}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_stop_allowed_when_no_agent_bound(agent):
    """Outcome 2: unbound session (AGENT_DIR is None) -> approve."""
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate({"stop": True}, agent_dir=None)
    assert (rc, decision) == (0, None)


def test_falsy_stop_does_not_deny(agent):
    """Outcome 1: `stop: false` is not a cancel."""
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate({"stop": False, "prompt": "check CI"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_rearm_on_running_loop_is_allowed(agent):
    """The rb-4345 re-arm -- the action the deny message tells you to take --
    must itself pass. A guard that blocks its own remedy is a wedge."""
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate(
        {"prompt": "<<autonomous-loop-dynamic>>"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_other_tools_are_untouched(agent):
    """The gate must ignore payloads from any tool but ScheduleWakeup."""
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate({"stop": True}, agent_dir=agent, tool_name="Bash")
    assert (rc, decision) == (0, None)


# ---------------------------------------------------------------- class C ---
# Outcome table from _arm_would_resurrect_nothing's docstring (guard-3328):
# every branch gets a case, not just the deny. Class B's twin, read together.

def test_arming_the_net_with_no_loop_under_it_is_denied(agent):
    """POSITIVE CONTROL. This is the exact call that looped sera.

    Measured, not asserted: unwiring `_arm_would_resurrect_nothing` from main()
    turns exactly THREE tests red -- this one, the message test below, and the
    mirror test at the end of the class. Every other test in this file stays
    green, class A and B included. So the guard's whole footprint is those
    three, and the five allow-cases here hold whether it exists or not, which
    is what makes them controls rather than restatements of it.
    """
    set_state(agent, "IDLE")
    rc, decision, out = run_gate(
        {"prompt": "<<autonomous-loop-dynamic>>"}, agent_dir=agent)
    assert (rc, decision) == (0, "deny")


def test_the_arm_denial_says_an_idle_agent_should_simply_stop(agent):
    """A deny that does not name the correct action sends the model looking."""
    set_state(agent, "IDLE")
    _, _, out = run_gate({"prompt": "<<autonomous-loop-dynamic>>"}, agent_dir=agent)
    assert "no loop under the net" in out
    assert "answer the user and stop" in out.replace("\\n", " ").replace("  ", " ")


def test_a_cancel_from_idle_is_not_an_arm_however_its_prompt_reads(agent):
    """Outcome 1, and a fresh-eyes regression (2026-09-22).

    ScheduleWakeup's contract: when `stop` is true "all other fields are
    ignored". So a sentinel left in `prompt` on a cancel is vestigial -- the
    model re-sending its previous args -- and reading it as an arm DENIES a
    legitimate cancel. That matters more than it sounds: cancelling a leftover
    net from IDLE is the manual remedy for the very cycle the arm guard exists
    to prevent, so the guard would have blocked its own fallback.
    """
    set_state(agent, "IDLE")
    rc, decision, _ = run_gate(
        {"stop": True, "prompt": "<<autonomous-loop-dynamic>>"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_a_wakeup_that_waits_on_the_world_is_not_the_loops_net(agent):
    """Outcome 1: only the sentinel claims to resurrect the loop. An external
    wait is legitimate from IDLE -- that is what assistant mode does all day."""
    set_state(agent, "IDLE")
    rc, decision, _ = run_gate(
        {"prompt": "check GitHub PR #142 CI run status"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_a_user_loop_continuation_is_not_the_loops_net(agent):
    """Outcome 1: /loop is the user's, and it may well be armed from IDLE."""
    set_state(agent, "IDLE")
    rc, decision, _ = run_gate(
        {"prompt": "/loop investigate flaky test"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_arming_is_allowed_when_agent_state_is_unreadable(agent):
    """Outcome 2: fail-open. A gate that guessed here would stall live loops."""
    rc, decision, _ = run_gate(
        {"prompt": "<<autonomous-loop-dynamic>>"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_arming_is_allowed_when_no_agent_is_bound(agent):
    """Outcome 2: no sid and no override -> nothing to read -> approve."""
    rc, decision, _ = run_gate({"prompt": "<<autonomous-loop-dynamic>>"})
    assert (rc, decision) == (0, None)


def test_a_stop_in_flight_still_keeps_its_net(agent):
    """Outcome 3 at its hardest: /stop writes `stop-requested` and LEAVES the
    state RUNNING until Phase -1.4. The loop is still finishing its in-flight
    obligations there, so the net must stay armable right through the stop."""
    set_state(agent, "RUNNING")
    (agent / "session" / "stop-requested").write_text("1", encoding="utf-8")
    rc, decision, _ = run_gate(
        {"prompt": "<<autonomous-loop-dynamic>>"}, agent_dir=agent)
    assert (rc, decision) == (0, None)


def test_the_two_guards_are_mirrors_of_one_another(agent):
    """The pair's whole claim: the net exists exactly when a loop does. One
    state file, two directions, and neither state leaves both doors open."""
    sentinel = {"prompt": "<<autonomous-loop-dynamic>>"}
    set_state(agent, "RUNNING")
    assert run_gate(sentinel, agent_dir=agent)[1] is None, "RUNNING may arm"
    assert run_gate({"stop": True}, agent_dir=agent)[1] == "deny", "RUNNING may not cancel"
    set_state(agent, "IDLE")
    assert run_gate(sentinel, agent_dir=agent)[1] == "deny", "IDLE may not arm"
    assert run_gate({"stop": True}, agent_dir=agent)[1] is None, "IDLE may cancel"


# ---------------------------------------------------------------- class D ---
# The IDLE refusal's TEXT mid-stop (). D1 sets IDLE long before D7
# finishes the stop, and "this turn ends normally" ended a vessel's turn mid-D4.
# Real SID resolution and the three call shapes are executed in
# test_stop_in_progress.py; these pin the text contract at the gate's own level.

def _stop_midway(agent, sid="sid-stop"):
    s = agent / "session"
    set_state(agent, "IDLE")
    for name, value in (("agent-mode", "autonomous"), ("stop-target-mode", "assistant"),
                        ("running-session-id", sid), ("latest-session-id", sid)):
        (s / name).write_text(value, encoding="utf-8")


def test_mid_stop_the_refusal_names_the_continuation(agent):
    _stop_midway(agent)
    rc, decision, out = run_gate({"prompt": "<<autonomous-loop-dynamic>>"},
                                 agent_dir=agent, session_id="sid-stop")
    assert (rc, decision) == (0, "deny"), "arming is still refused mid-stop"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "graceful stop is NOT finished" in reason
    assert "aspirations-graceful-stop" in reason
    assert "ends normally" not in reason


def test_without_a_stop_in_progress_the_refusal_is_byte_identical(agent):
    """The same bound session, IDLE, but the stop finished (D7 deleted the target)."""
    _stop_midway(agent)
    (agent / "session" / "stop-target-mode").unlink()
    _, decision, out = run_gate({"prompt": "<<autonomous-loop-dynamic>>"},
                                agent_dir=agent, session_id="sid-stop")
    assert decision == "deny"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == _load_gate_module().ARM_DENY_REASON


def test_mid_stop_text_is_scoped_to_the_stopping_session(agent):
    _stop_midway(agent)
    _, decision, out = run_gate({"prompt": "<<autonomous-loop-dynamic>>"},
                                agent_dir=agent, session_id="sid-observer")
    assert decision == "deny"
    reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == _load_gate_module().ARM_DENY_REASON


# ---------------------------------------------------------------- class A ---
# Pre-existing behavior, previously untested. Pins it against the new branch.

@pytest.mark.parametrize("prompt", [
    "/aspirations loop", "/boot", "/respond", "/reflect", "/review-hypotheses",
])
def test_user_invocable_slash_prompts_denied(prompt, agent):
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate({"prompt": prompt}, agent_dir=agent)
    assert (rc, decision) == (0, "deny")


@pytest.mark.parametrize("prompt", [
    "<<autonomous-loop-dynamic>>",
    "/loop investigate flaky test",
    "check GitHub PR #142 CI run status",
])
def test_sanctioned_prompts_allowed(prompt, agent):
    set_state(agent, "RUNNING")
    rc, decision, _ = run_gate({"prompt": prompt}, agent_dir=agent)
    assert (rc, decision) == (0, None)


# ------------------------------------------------- resolution mechanism ---
# The guard is only worth anything if it resolves the agent the way PRODUCTION
# does. An env-based resolve (MIND_AGENT) would make it silently INERT on this
# hook -- that var is injected by the PreToolUse[Bash] hook, and this fires on
# ScheduleWakeup. These pin the payload-session_id path so it cannot regress.

def _load_gate_module():
    import importlib.util
    sys.path.insert(0, str(GATE.parent))
    spec = importlib.util.spec_from_file_location("swg", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_dir_resolves_through_sid_not_env(monkeypatch, tmp_path):
    """Production path: payload session_id -> agent name -> agent dir."""
    mod = _load_gate_module()
    monkeypatch.delenv("MIND_AGENT_DIR", raising=False)
    monkeypatch.setenv("MIND_AGENT", "wrong-agent-from-env")

    import _resolve_agent_from_sid, _paths
    monkeypatch.setattr(_resolve_agent_from_sid, "resolve",
                        lambda sid, root: "right-agent" if sid == "sid-123" else "")
    monkeypatch.setattr(_paths, "agent_dir", lambda name: tmp_path / name)

    got = mod._session_dir("sid-123")
    assert got == tmp_path / "right-agent" / "session", (
        "must resolve via session_id, not MIND_AGENT")


def test_session_dir_none_without_sid(monkeypatch):
    """No sid and no override -> None -> fail-open (outcome 2)."""
    mod = _load_gate_module()
    monkeypatch.delenv("MIND_AGENT_DIR", raising=False)
    monkeypatch.setenv("MIND_AGENT", "zeta")
    assert mod._session_dir("") is None


def test_malformed_payload_approves():
    """Fail-open on garbage stdin."""
    proc = subprocess.run(
        [sys.executable, str(GATE)], input="not json",
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
