"""Pins the body filter on the two stop-hook turn-end gates ().

DEFECT. ``agents/<a>/session/pending-agents.yaml`` (Gate 2.5) and
``.../background-jobs.yaml`` (Gate 2.6) are AGENT-WIDE — ``session/`` singular,
one file per mind on the box, shared by every body of that mind — and carried no
body key. Both gates ``exit 0`` = ALLOW the turn-end when their store reports a
pending item, so an item registered by a WORKER body made the REDUCER's stop-hook
ALLOW a turn-end it would otherwise BLOCK. That does not kill a healthy loop (the
reducer's terminal ``Skill(aspirations)`` queues the next turn regardless) — it
removes the NET under a TEXT-death, bounding an outage at the ~600s deadman
wakeup and leaving it UNBOUNDED wherever ``session/deadman-disabled`` is set.

A COMPLIANT worker triggers this: ``WORKER_PHASES = (select, claim, execute)``
and every ``background-jobs.sh register`` call site is an EXECUTE-phase skill.

THE FOUR INVARIANTS PINNED HERE, and why each is load-bearing:

1. OPT-IN. With no ``--body-sid`` the verdict is byte-identical to before.
   ``recovery-gate.sh:658`` probes CROSS-AGENT
   (``MIND_AGENT="$agent" background-jobs.sh has-pending``) and asks an
   agent-wide question. Because its Cond 4 passes when has-pending exits 1,
   defaulting the filter ON would make zombie-recovery MORE likely to fire on an
   agent that is genuinely busy.
2. BOTH DIRECTIONS. guard-2290: a filter that PARTIALLY works is the dangerous
   case, and a count-based smell test cannot see it. Own-body items must still
   count (or the gates become permanently non-functional and the loop busy-spins
   through every legitimate external wait); sibling-body items must not.
3. UNKNOWN OWNER IS NOT AN ALLOW. A record with a missing/empty ``owner_sid``
   (registered before this change) does not gate. rb-605 — anticipation gates
   fail OPEN — and Gate 2.6's own comment: an error resolves to "no pending
   jobs" so the BLOCK proceeds and the loop stays alive. An ALLOW is precisely
   what removes the net, so filtering must never turn an unknown into one.
4. THE FILTER MUST NOT DELETE. ``pending-agents.cmd_has_pending`` writes the
   staleness-pruned list back to the SHARED file. Filtering before that write
   would silently destroy the sibling body's registrations as a side effect of
   merely asking whether THIS body has work pending.

The stop-hook passes ``--body-sid "$HOOK_SID"`` rather than reading ``$MIND_SID``:
that variable is injected only into Bash tool calls (``bash-agent-inject.py``)
and is absent from the hook environment, so an env-read implementation would
resolve empty in production and — under invariant 3 — silently disable both
gates while testing green by hand. ``test_stop_hook_passes_body_sid_to_both_gates``
is what holds that call shape in place.
"""
import argparse
import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, str(SCRIPTS / filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bgjobs = _load("bgjobs_bodyfilter", "background-jobs.py")
pagents = _load("pagents_bodyfilter", "pending-agents.py")

REDUCER = "sid-reducer-1111"
WORKER = "sid-worker-2222"


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def jobs_path(tmp_path, monkeypatch):
    p = tmp_path / "session" / "background-jobs.yaml"
    monkeypatch.setattr(bgjobs, "JOBS_PATH", p)
    monkeypatch.setattr(bgjobs, "pid_alive", lambda pid: True)
    return p


@pytest.fixture
def agents_path(tmp_path, monkeypatch):
    p = tmp_path / "session" / "pending-agents.yaml"
    monkeypatch.setattr(pagents, "PENDING_PATH", p)
    return p


def _job_args(job_id, body_sid):
    return argparse.Namespace(
        id=job_id, type="test", goal="g-test", pid=4242,
        monitor_goal="g-mon", completion_check="", metadata=None,
        output_artifacts=None, body_sid=body_sid,
    )


def _agent_args(agent_id, body_sid):
    return argparse.Namespace(
        id=agent_id, team="t", goal="g-test", purpose="p",
        timeout=10, body_sid=body_sid,
    )


def _has_pending(mod, body_sid=None):
    """Return True when the gate would ALLOW the turn-end (exit 0)."""
    ns = argparse.Namespace() if body_sid is None else argparse.Namespace(body_sid=body_sid)
    with pytest.raises(SystemExit) as exc:
        mod.cmd_has_pending(ns)
    return exc.value.code == 0


# ------------------------------------------------- invariant 2: BOTH directions

@pytest.mark.parametrize("mod_name", ["bgjobs", "pagents"])
def test_own_body_item_still_gates(mod_name, jobs_path, agents_path, capsys):
    """The healthy case must be preserved, or the gates stop working entirely."""
    mod, reg, args = _dispatch(mod_name)
    reg(args("x1", REDUCER))
    capsys.readouterr()
    assert _has_pending(mod, REDUCER) is True, (
        "this body's own pending item no longer gates turn-end — the gate is "
        "now permanently non-functional and every legitimate external wait "
        "will busy-spin against a BLOCK"
    )


@pytest.mark.parametrize("mod_name", ["bgjobs", "pagents"])
def test_sibling_body_item_does_not_gate(mod_name, jobs_path, agents_path, capsys):
    """The defect itself: a worker body's item must not ALLOW the reducer's turn-end."""
    mod, reg, args = _dispatch(mod_name)
    reg(args("x1", WORKER))
    capsys.readouterr()
    assert _has_pending(mod, REDUCER) is False, (
        "a sibling body's pending item ALLOWed this body's turn-end — the "
        "text-death net is removed (g-306-135)"
    )


# ----------------------------------------- invariant 3: unknown owner != ALLOW

@pytest.mark.parametrize("mod_name", ["bgjobs", "pagents"])
def test_legacy_record_without_owner_does_not_gate(mod_name, jobs_path, agents_path, capsys):
    """A pre-change record has no owner_sid. Unknown owner must fail toward BLOCK."""
    mod, reg, args = _dispatch(mod_name)
    reg(args("x1", ""))          # empty body_sid + no MIND_SID -> owner_sid ""
    capsys.readouterr()
    assert _has_pending(mod, REDUCER) is False, (
        "a record with an unresolvable owner was counted as this body's, "
        "turning an unknown into an ALLOW (rb-605 — must fail toward BLOCK)"
    )


@pytest.mark.parametrize("mod_name", ["bgjobs", "pagents"])
def test_empty_caller_sid_does_not_silently_match_legacy_records(
    mod_name, jobs_path, agents_path, capsys
):
    """An empty --body-sid must NOT string-match a legacy empty owner_sid.

    This is why the flag's default is None rather than "": ABSENT and EMPTY mean
    opposite things. Absent = "I did not ask to filter" (recovery-gate, agent-wide).
    Empty = "I asked to filter but could not resolve my own identity" — an error,
    and `"" == ""` would otherwise match every un-owned record by string equality
    and ALLOW. That is the error-becomes-ALLOW inversion invariant 3 forbids,
    reached by a different route than the test above.

    Concretely: stop-hook passes `--body-sid "$HOOK_SID"`. If HOOK_SID ever came
    back empty, this branch is what keeps the gate from ALLOWing everything.
    """
    mod, reg, args = _dispatch(mod_name)
    reg(args("x1", ""))
    capsys.readouterr()
    assert _has_pending(mod, "") is False, (
        "an identity-less caller (--body-sid '') matched a legacy empty "
        "owner_sid and ALLOWed the turn-end — the error case became an ALLOW"
    )


# --------------------------------------------- invariant 1: opt-in / recovery-gate

@pytest.mark.parametrize("mod_name", ["bgjobs", "pagents"])
def test_no_body_sid_is_agent_wide_legacy_behaviour(mod_name, jobs_path, agents_path, capsys):
    """recovery-gate.sh Cond 4 calls has-pending with no flag and cross-agent."""
    mod, reg, args = _dispatch(mod_name)
    reg(args("x1", WORKER))
    capsys.readouterr()
    assert _has_pending(mod, None) is True, (
        "the unfiltered call stopped seeing another body's job — recovery-gate "
        "Cond 4 would now pass and zombie-recovery could fire on a busy agent"
    )


# ------------------------------------------- invariant 4: the filter must not delete

def test_body_filtered_check_does_not_delete_sibling_registrations(agents_path, capsys):
    """has-pending prunes+writes the SHARED file; filtering must happen after.

    One live worker agent and one stale worker agent. The reducer asks whether
    IT has work pending (answer: no). The stale entry is correctly pruned, but
    the LIVE worker entry must survive — otherwise merely asking the question
    destroyed the sibling body's registration.
    """
    fresh = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    stale = (datetime.now() - timedelta(minutes=99)).strftime("%Y-%m-%dT%H:%M:%S")
    pagents.write_data({"agents": [
        {"agent_id": "live-worker", "team_name": "t", "goal_id": "g",
         "dispatched_at": fresh, "purpose": "", "timeout_minutes": 10,
         "owner_sid": WORKER},
        {"agent_id": "stale-worker", "team_name": "t", "goal_id": "g",
         "dispatched_at": stale, "purpose": "", "timeout_minutes": 10,
         "owner_sid": WORKER},
    ]})

    assert _has_pending(pagents, REDUCER) is False
    capsys.readouterr()

    remaining = {a["agent_id"] for a in pagents.read_data()["agents"]}
    assert "live-worker" in remaining, (
        "the sibling body's LIVE registration was deleted by a body-filtered "
        "has-pending — the filter ran before the prune write-back"
    )
    assert "stale-worker" not in remaining, (
        "staleness pruning stopped happening — this test is no longer "
        "exercising the write-back path it exists to protect"
    )


# ------------------------------------------------- the production call shape

def test_stop_hook_passes_body_sid_to_both_gates():
    """Both gates must pass an EXPLICIT SID, and it must be HOOK_SID.

    MIND_SID is injected only into Bash tool calls, never into the hook
    environment, so an env-read implementation resolves empty in production and
    (per invariant 3) disables both gates while hand-testing green. This test is
    the only thing standing between that and a silent fleet-wide regression.
    """
    src = (SCRIPTS / "stop-hook.sh").read_text(encoding="utf-8")
    for wrapper in ("pending-agents.sh", "background-jobs.sh"):
        line = next(
            (l for l in src.splitlines() if wrapper in l and "has-pending" in l), None
        )
        assert line is not None, f"no has-pending call site found for {wrapper}"
        assert "--body-sid" in line, (
            f"stop-hook's {wrapper} has-pending call lost --body-sid — the gate "
            f"is agent-wide again and a sibling body can ALLOW this body's turn-end"
        )
        assert "$HOOK_SID" in line, (
            f"stop-hook's {wrapper} call passes something other than $HOOK_SID; "
            f"MIND_SID is not set in the hook environment"
        )


def _dispatch(mod_name):
    if mod_name == "bgjobs":
        return bgjobs, bgjobs.cmd_register, _job_args
    return pagents, pagents.cmd_register, _agent_args
