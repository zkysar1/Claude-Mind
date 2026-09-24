"""Coverage for : a graceful stop left unfinished between D1 and D7.

THE DEFECT, MEASURED (2026-09-24, DEV vessel, g-373-128 outcome-3 run). The mind
ran graceful-stop D1-D3 and was part-way through D4 when schedule-wakeup-gate
refused its deadman re-arm with "Nothing. You are IDLE ... this turn ends
normally". It ended the turn, and stop-hook Gate 1 ALLOWed it, because Gate 1
ALLOWs every non-RUNNING turn-end. Step 9's handoff and D4.5-D7 never ran.

WHAT THIS FILE PINS
-------------------
1. ``stop_in_progress.classify`` -- every clause of the predicate gets a case
   (guard-3328), including the two SID sources on either side of D6.
2. The bound -- ``count_blocks`` and ``decide`` -- and the text, which must be
   TRUE FOR THIS TURN (rb-11311): it names what D6 and the handoff have and have
   not done, and never tells the model its turn may end.
3. stop-hook.sh Gate 0-stop, EXECUTED in a throwaway PROJECT_ROOT through the
   shared harness of ``test_stop_hook_gate_integration`` (same fixture, same
   production-shaped env): the stopping session is held before and after D6, no
   other session is ever held, the look-alikes pass straight through, the veto
   is bounded, and a complete D1..D7.1 sequence ends with no extra BLOCK.
4. Call shapes: the Claude Code Stop event (no agent env var), the fleet's
   ``MIND_AGENT``, and a vessel's ``MIND_AGENT`` with cwd = the workspace root
   all reach the same verdict, for the hook AND for schedule-wakeup-gate --
   because both resolve the agent from the payload SID and neither reads an
   agent-name env var.
5. A mutation proof: with the gate neutralized, the measured defect comes back
   (the mid-stop turn-end is ALLOWed by Gate 1).

The runner below is NOT the harness's ``_run_hook_as_runner``: that one pins the
session id to the runner's and offers no cwd, and these tests need both the
observer's SID and the vessel's cwd. The env handling is the same (MIND_SID /
MIND_AGENT scrubbed per guard-1742, STORAGE_BACKEND=local per guard-955).

Run: STORAGE_BACKEND=local python3 -m pytest \\
       core/scripts/tests/test_stop_in_progress.py -q
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
for _p in (str(SCRIPTS), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stop_in_progress as sip  # noqa: E402
from _bash_helpers import BASH  # noqa: E402
from test_stop_hook_gate_integration import (  # noqa: E402
    AGENT, RUNNER_SID, _agent_dir, _blocked, _build_runner_root, _hook_log)

OBSERVER_SID = "observer-sid-1212-3434-5656"
# The hook's gate line the mutation proof edits, pinned against the real hook
# below so a reword can never turn the mutation into a silent no-op.
GATE_LINE = 'if [ -f "$HOOK_AGENT_DIR/session/stop-target-mode" ] \\'


# ── the predicate ────────────────────────────────────────────────────────────

def _ev(**over):
    base = {"state": "IDLE", "mode": "autonomous", "target_mode_mtime": 1000.0,
            "stop_requested": False, "runner_sid": "S", "latest_sid": "S"}
    base.update(over)
    return base


@pytest.mark.parametrize("over,sid,expected", [
    ({}, "S", (True, "unfinished")),
    ({}, "", (False, "no-sid")),
    ({"target_mode_mtime": None}, "S", (False, "no-stop")),
    ({"state": None}, "S", (False, "state-unreadable")),
    ({"state": "RUNNING"}, "S", (False, "running")),
    ({"stop_requested": True}, "S", (False, "unconsumed")),
    ({"mode": "assistant"}, "S", (False, "mode-set")),
    ({"mode": None}, "S", (False, "mode-set")),
    ({"runner_sid": "", "latest_sid": ""}, "S", (False, "no-stopping-sid")),
    ({}, "OTHER", (False, "other-session")),
    # After D6 deleted running-session-id, latest-session-id scopes the stop.
    ({"runner_sid": ""}, "S", (True, "unfinished")),
    ({"runner_sid": "", "latest_sid": "OTHER"}, "S", (False, "other-session")),
    # While running-session-id exists it is the runner identity, as at Gate 0.
    ({"latest_sid": "OTHER"}, "S", (True, "unfinished")),
])
def test_classify_outcome_table(over, sid, expected):
    assert sip.classify(_ev(**over), sid) == expected


def test_read_evidence_on_an_empty_dir_is_not_a_stop(tmp_path):
    """Every file absent -> fail-safe: the predicate is FALSE."""
    ev = sip.read_evidence(tmp_path)
    assert sip.classify(ev, "S") == (False, "no-stop")


# ── the bound ────────────────────────────────────────────────────────────────

def test_count_blocks_counts_only_this_gate_for_this_sid_since_the_stop():
    since = dt.datetime(2026, 9, 24, 13, 34, 1, 700000).timestamp()
    log = "\n".join([
        "2026-09-24T13:30:00 BLOCK gate=stop-unfinished sid=S agent=a n=1/3",
        "2026-09-24T13:34:01 BLOCK gate=stop-unfinished sid=S agent=a n=1/3",
        "2026-09-24T13:35:40 BLOCK gate=stop-unfinished sid=SX agent=a n=1/3",
        "2026-09-24T13:35:41 BLOCK gate=stop-unfinished sid=T agent=a n=1/3",
        "2026-09-24T13:36:00 BLOCK sid=S agent=a runner_token=",
        "2026-09-24T13:36:10 ALLOW gate=not-running sid=S agent=a state=IDLE",
        "not-a-time BLOCK gate=stop-unfinished sid=S agent=a n=2/3",
        "2026-09-24T13:37:00 BLOCK gate=stop-unfinished sid=S agent=a n=2/3",
    ])
    # 13:30 predates the stop, SX/T are other sessions, the loop's own BLOCK and
    # the ALLOW are other gates, the garbage stamp is skipped; 13:34:01 is the
    # stop request's own second and counts (floored), 13:37 counts.
    assert sip.count_blocks(log, "S", since) == 2


@pytest.mark.parametrize("unfinished,prior,expected", [
    (False, 0, "pass"), (False, 9, "pass"),
    (True, 0, "block"), (True, sip.MAX_BLOCKS - 1, "block"),
    (True, sip.MAX_BLOCKS, "cap"), (True, sip.MAX_BLOCKS + 4, "cap"),
])
def test_decide(unfinished, prior, expected):
    assert sip.decide(unfinished, prior) == expected


# ── the text is true for this turn (rb-11311) ────────────────────────────────

def test_block_reason_names_the_continuation_and_never_lets_the_turn_end():
    reason = sip.block_reason(_ev(), "absent", 1, 3)
    assert "GRACEFUL STOP NOT FINISHED" in reason
    assert "block 1 of 3" in reason
    assert "the rest of D4" in reason and "D7.1" in reason
    assert "Skill(aspirations-graceful-stop)" in reason and "--resume" in reason
    assert "No handoff has been written during this stop" in reason
    assert "ends normally" not in reason


def test_the_text_tracks_d6_and_the_handoff():
    before = sip.block_reason(_ev(), "fresh", 1, 3)
    assert "D6 has not run yet" in before
    assert "stop-handoff-check will pass" in before
    after = sip.block_reason(_ev(runner_sid=""), "stale", 2, 3)
    assert "D6 has run" in after
    assert "the rest of D4" not in after, "D4 is behind a session that reached D6"
    assert after.index("continuation handoff") < after.index("D7, D7.05")


def test_the_arm_refusal_is_a_continuation_not_an_ending():
    text = sip.arm_refusal(_ev(), "absent")
    assert "there is no loop under the net" in text
    assert "Do NOT end the turn" in text
    assert "graceful stop is NOT finished" in text
    assert "ends normally" not in text


# ── fail-open: a fault never holds a turn ────────────────────────────────────

def test_main_fails_open_when_the_verdict_raises(monkeypatch, capsys, tmp_path):
    """The CLI's outer catch: any fault prints one pass line and no payload."""
    def boom(*_a, **_k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sip, "hook_verdict", boom)
    rc = sip.main(["--session-dir", str(tmp_path), "--sid", "S",
                   "--log", str(tmp_path / "absent.log")])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["pass error:RuntimeError"]


# ── stop-hook.sh Gate 0-stop, executed ───────────────────────────────────────

def _bind(root, sid, mode="assistant"):
    """A binding.yaml in the shape session-binding-write.py writes.

    The FULL shape matters for the wake-up gate: it resolves the agent through
    _session_binding, which refuses a binding whose `session_id` field is
    missing (reason session-id-mismatch). The hook's bash glob reads only the
    path, so the harness's two-field binding satisfies the hook alone.
    """
    d = _agent_dir(root) / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "binding.yaml").write_text(
        f"session_id: {sid}\nagent: {AGENT}\nmode: {mode}\n"
        "started_at: '2026-09-24T13:20:48'\nstarted_by: claude-code\n",
        encoding="utf-8")


def _root(tmp_path, state="IDLE"):
    world = tmp_path / "world"
    world.mkdir()
    meta = tmp_path / "meta_stop"
    meta.mkdir()
    root = _build_runner_root(tmp_path, world, meta, state)
    _bind(root, RUNNER_SID, mode="autonomous")
    return root


def _sess(root):
    return _agent_dir(root) / "session"


def _stop_midway(root, after_d6=False):
    """Where the measured vessel stood: D1-D3 done, D4 under way.

    stop-target-mode is written by the stop requester BEFORE its signal, D1 set
    IDLE, D3 consumed stop-requested (so it is absent), agent-mode is still the
    /start value, and latest-session-id is /start's runner triple-write.
    """
    sess = _sess(root)
    (sess / "stop-target-mode").write_text("assistant\n", encoding="utf-8")
    (sess / "agent-state").write_text("IDLE", encoding="utf-8")
    (sess / "agent-mode").write_text("autonomous", encoding="utf-8")
    (sess / "latest-session-id").write_text(RUNNER_SID, encoding="utf-8")
    if after_d6:
        (sess / "running-session-id").unlink()


def _finish_the_stop(root):
    """D4's Step 9 handoff, then D6, D7 and D7.1, as the file state they leave."""
    sess = _sess(root)
    (sess / "handoff.yaml").write_text("session_summary: consolidated\n",
                                       encoding="utf-8")
    for name in ("running-session-id", "stop-loop", "loop-active"):   # D6
        (sess / name).unlink(missing_ok=True)
    (sess / "agent-mode").write_text("assistant", encoding="utf-8")   # D7
    (sess / "stop-target-mode").unlink()                               # D7
    (sess / "stop-checkpoint.json").unlink(missing_ok=True)            # D7.1


def _env(shape):
    env = os.environ.copy()
    for k in ("MIND_SID", "MIND_AGENT", "MIND_AGENT_DIR", "MIND_AGENT"):
        env.pop(k, None)
    env["STORAGE_BACKEND"] = "local"
    if shape == "fleet":
        env["MIND_AGENT"] = AGENT
    elif shape == "vessel":
        env["MIND_AGENT"] = AGENT
    return env


def _fire(root, sid=RUNNER_SID, shape="stop-event"):
    """Fire the hook. `vessel` also runs it from the workspace root, as a vessel does."""
    return subprocess.run(
        [BASH, str(root / "core" / "scripts" / "stop-hook.sh")],
        input=json.dumps({"session_id": sid}), capture_output=True, text=True,
        timeout=180, env=_env(shape), cwd=str(root) if shape == "vessel" else None)


def _reason(proc):
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if "reason" in payload:
                return payload["reason"]
    raise AssertionError(f"no decision payload in stdout: {proc.stdout!r}")


def test_mid_stop_turn_end_is_blocked_with_the_continuation(tmp_path):
    """POSITIVE CONTROL -- the measured vessel state, before D6."""
    root = _root(tmp_path)
    _stop_midway(root)
    proc = _fire(root)
    log = _hook_log(root)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _blocked(proc), f"stdout:\n{proc.stdout}\nlog:\n{log}"
    reason = _reason(proc)
    assert "GRACEFUL STOP NOT FINISHED" in reason and "block 1 of 3" in reason
    assert "D6 has not run yet" in reason
    assert f"BLOCK gate=stop-unfinished sid={RUNNER_SID} agent={AGENT} n=1/3" in log
    assert "gate=not-running" not in log, "Gate 1 must not be reached"
    assert not (_sess(root) / "compact-pending").exists(), (
        "compact-pending belongs to the loop's BLOCK path, not this one")


def test_after_d6_the_stopping_session_is_still_held(tmp_path):
    """D6 deletes running-session-id, so Gate 0 would ALLOW gate=no-runner."""
    root = _root(tmp_path)
    _stop_midway(root, after_d6=True)
    proc = _fire(root)
    log = _hook_log(root)

    assert _blocked(proc), f"log:\n{log}"
    assert "D6 has run" in _reason(proc)
    assert "gate=no-runner" not in log


@pytest.mark.parametrize("after_d6,allow_gate", [
    (False, "gate=sid-mismatch"), (True, "gate=no-runner")])
def test_another_session_is_never_held(tmp_path, after_d6, allow_gate):
    root = _root(tmp_path)
    _stop_midway(root, after_d6=after_d6)
    _bind(root, OBSERVER_SID)
    proc = _fire(root, sid=OBSERVER_SID)
    log = _hook_log(root)

    assert not _blocked(proc), f"an observer was held; log:\n{log}"
    assert allow_gate in log
    assert "stop-unfinished" not in log


@pytest.mark.parametrize("tweak,why", [
    (lambda s: (s / "stop-target-mode").unlink(), "no stop in progress"),
    (lambda s: (s / "stop-requested").write_text("1", encoding="utf-8"),
     "request not yet consumed by a stop handler"),
    (lambda s: (s / "agent-mode").write_text("assistant", encoding="utf-8"),
     "the mode was already set"),
])
def test_look_alikes_pass_through_to_gate_1_unchanged(tmp_path, tweak, why):
    root = _root(tmp_path)
    _stop_midway(root)
    tweak(_sess(root))
    proc = _fire(root)
    log = _hook_log(root)

    assert not _blocked(proc), f"{why}: held; log:\n{log}"
    assert "gate=not-running" in log, f"{why}: Gate 1 not reached; log:\n{log}"
    assert "stop-unfinished" not in log and "STOP-UNFINISHED" not in log


def test_a_crashing_helper_fails_open_to_gate_1(tmp_path):
    """A helper that dies before main() prints nothing: the hook ALLOWs, never holds."""
    root = _root(tmp_path)
    (root / "core" / "scripts" / "stop_in_progress.py").write_text(
        'raise RuntimeError("helper broken")\n', encoding="utf-8")
    _stop_midway(root)
    proc = _fire(root)
    log = _hook_log(root)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not _blocked(proc), f"a broken helper held the turn; log:\n{log}"
    assert "gate=not-running" in log, f"Gate 1 not reached; log:\n{log}"
    assert "stop-unfinished" not in log and "STOP-UNFINISHED" not in log


def test_the_veto_is_bounded(tmp_path):
    """MAX_BLOCKS BLOCKs, then the turn-end falls through to Gate 1 and is logged."""
    root = _root(tmp_path)
    _stop_midway(root)
    cap = sip.MAX_BLOCKS
    procs = [_fire(root) for _ in range(cap + 2)]
    log = _hook_log(root)

    assert [_blocked(p) for p in procs] == [True] * cap + [False, False], log
    for n in range(1, cap + 1):
        assert f"n={n}/{cap}" in log
    assert log.count("BLOCK gate=stop-unfinished") == cap
    assert log.count(f"STOP-UNFINISHED-CAP sid={RUNNER_SID}") == 2
    assert f"n={cap}/{cap} -- turn-end allowed below" in log
    assert log.count("ALLOW gate=not-running") == 2


def test_a_complete_stop_ends_its_turn_with_no_block(tmp_path):
    """Outcome 3: a stop that reaches D7.1 is allowed, and finishing clears the veto."""
    root = _root(tmp_path)
    _stop_midway(root)
    assert _blocked(_fire(root)), "precondition: held while unfinished"

    _finish_the_stop(root)
    proc = _fire(root)
    log = _hook_log(root)

    assert not _blocked(proc), f"log:\n{log}"
    assert "gate=no-runner" in log
    assert log.count("BLOCK gate=stop-unfinished") == 1
    assert "STOP-UNFINISHED-CAP" not in log


def test_a_stop_run_straight_through_never_meets_the_gate(tmp_path):
    """Outcome 3, clean: the only turn-end of an uninterrupted stop is after D7.1."""
    root = _root(tmp_path)
    _stop_midway(root)
    _finish_the_stop(root)
    proc = _fire(root)
    log = _hook_log(root)

    assert not _blocked(proc)
    assert "stop-unfinished" not in log and "STOP-UNFINISHED" not in log


@pytest.mark.parametrize("shape", ["stop-event", "fleet", "vessel"])
def test_every_call_shape_holds_the_stopping_session(tmp_path, shape):
    root = _root(tmp_path)
    _stop_midway(root)
    proc = _fire(root, shape=shape)
    assert _blocked(proc), f"{shape}: log:\n{_hook_log(root)}"
    assert "block 1 of 3" in _reason(proc)


# ── schedule-wakeup-gate in the same root: real SID resolution ───────────────

def _gate(root, sid, shape):
    payload = {"tool_name": "ScheduleWakeup", "session_id": sid,
               "tool_input": {"prompt": "<<autonomous-loop-dynamic>>",
                              "delaySeconds": 600}}
    proc = subprocess.run(
        [sys.executable, str(root / "core" / "scripts" / "schedule-wakeup-gate.py")],
        input=json.dumps(payload), capture_output=True, text=True, timeout=60,
        env=_env(shape), cwd=str(root) if shape == "vessel" else None)
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


@pytest.mark.parametrize("shape", ["stop-event", "fleet", "vessel"])
def test_the_gate_names_the_continuation_to_the_stopping_session(tmp_path, shape):
    root = _root(tmp_path)
    _stop_midway(root)
    decision, text = _gate(root, RUNNER_SID, shape)
    assert decision == "deny", "arming is still refused mid-stop"
    assert "graceful stop is NOT finished" in text
    assert "ends normally" not in text


def test_the_gate_keeps_its_idle_text_for_any_other_session(tmp_path):
    root = _root(tmp_path)
    _stop_midway(root)
    _bind(root, OBSERVER_SID)
    decision, text = _gate(root, OBSERVER_SID, "stop-event")
    assert decision == "deny"
    assert "this turn ends normally" in text
    assert "graceful stop is NOT finished" not in text


# ── mutation proof ───────────────────────────────────────────────────────────

def test_the_gate_line_is_pinned_to_the_real_hook():
    hook = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    assert hook.count(GATE_LINE) == 1


def test_mutation_neutralizing_the_gate_reproduces_the_measured_defect(tmp_path):
    root = _root(tmp_path)
    hook = root / "core" / "scripts" / "stop-hook.sh"
    text = hook.read_text(encoding="utf-8")
    assert text.count(GATE_LINE) == 1
    hook.write_text(text.replace(GATE_LINE, "if false \\"), encoding="utf-8")
    _stop_midway(root)
    proc = _fire(root)
    log = _hook_log(root)

    assert not _blocked(proc), "the mutation did not disable the gate"
    assert "gate=not-running" in log, (
        "without Gate 0-stop the mid-stop turn-end is ALLOWed by Gate 1 -- "
        f"the 2026-09-24 vessel defect; log:\n{log}")
