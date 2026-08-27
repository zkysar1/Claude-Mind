#!/usr/bin/env python3
"""Regression tests for core/scripts/schedule-wakeup-gate.py.

Two independent failure classes are covered:

A. SLASH-PREFIX prompts (2026-05-18) -- the gate's original purpose. This half
   shipped untested; these tests pin it.
B. `stop: true` cancelling the deadman net on a LIVE loop (2026-08-25). The
   deadman is a SINGLE replace-slot wakeup, so cancelling it while the agent is
   RUNNING converts a recoverable pause into a hard stop that needs a human.

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


def run_gate(tool_input, agent_dir=None, tool_name="ScheduleWakeup"):
    """Invoke the gate as production does. Returns (rc, decision_or_None)."""
    env = dict(os.environ)
    env.pop("MIND_AGENT", None)
    if agent_dir is not None:
        env["MIND_AGENT_DIR"] = str(agent_dir)
    else:
        env.pop("MIND_AGENT_DIR", None)
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
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
