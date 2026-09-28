"""Phase 2A (): worker-body simplified execution contract.

worker_execute.py defines the worker's phase split -- a WORKER Body runs
select/claim/execute and SKIPS the reducer-only encode/reflect/consolidate
phases (verify, spark, complete-review, state-update, evolution, learning-gate,
productivity-check) -- and the reducer-aware WM routing: a worker writes ONLY
its own forked Body WM when the Body forked one (the same Phase-1A activation
signal as wm.py / agent_paths.py -- the forked body-WM-file's existence), else
the agent-wide WM. With one Body (the reducer) or no unit_key, the routing
collapses to today's agent-wide path (dormant until a 2nd Body forks).

Daemon-safe (no daemon_integration marker -- pure contract + path arithmetic;
the WM-routing cases use a tmp project_root override, the body-merge.py pattern).

Run:
  python -m pytest core/scripts/tests/test_worker_execute.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


we = _load("worker_execute", "worker_execute.py")

SID_A = "55555555-5555-4555-8555-555555555555"
SID_B = "66666666-6666-4666-8666-666666666666"


# ----------------------- phase contract -----------------------

def test_worker_runs_select_claim_execute():
    for p in ("select", "claim", "execute"):
        assert we.worker_should_run_phase(p), f"worker must run {p}"
    assert we.WORKER_PHASES == ("select", "claim", "execute", "verify-own-unit")


def test_worker_runs_verify_own_unit_but_not_the_reducer_verify_phase():
    """: the per-unit half of verification moved to the worker; the
    reducer keeps the residue.

    Both halves are asserted together ON PURPOSE. The whole safety property of
    the split is that these two phases are DIFFERENT SCOPES, so a change that
    collapsed them -- e.g. "simplifying" by letting a worker run `verify` --
    would keep the first assertion green while destroying the design. Pinning
    the negative beside the positive is what makes that regression loud.
    """
    assert we.worker_should_run_phase("verify-own-unit")
    assert not we.worker_should_run_phase("verify")
    assert "verify" in we.REDUCER_ONLY_PHASES
    assert "verify-own-unit" not in we.REDUCER_ONLY_PHASES


def test_verify_own_unit_is_a_scoped_call_naming_its_mode():
    """It must be a SCOPED_CALL into the existing verify skill (guard-1867 /
    guard-2676: invoke the component, never transcribe its steps), and a
    scoped call is only distinguishable from a transcription by naming the
    mode INSIDE that component."""
    d = we.LIFECYCLE_DISPOSITIONS["verify-own-unit"]
    assert d.kind == we.SCOPED_CALL
    assert "aspirations-verify" in d.target
    assert (d.mode or "").strip(), "a scoped call must name its mode"
    assert "own-unit" in d.mode


def test_worker_loop_phase_4a_invokes_the_scoped_verify_before_the_close():
    """The WIRING pin. It replaces the pending-marker tripwire that stood here
    until 2026-09-03 (g-306-417 part1b), which was written to fail the moment
    the loop invoked the skill — it did, so the tripwire was discharged and this
    took its place. A retired tripwire deleted without a successor would leave
    the declaration asserted by nothing.

    Pinning SKILL.md TEXT is the right level here, not the guard-2195
    two-ends-agree-on-a-name anti-pattern: worker-loop/SKILL.md IS the executable
    artifact for this phase — an LLM reads those lines and acts on them — so the
    text is the behaviour rather than a proxy for it.

    ORDER IS THE LOAD-BEARING HALF. Judgement must precede the mechanical status
    write: `iteration-close.sh --phase verify` is the only writer of the status
    transition (guard-2523), so a verify invoked AFTER it would grade a goal that
    is already closed and could not affect `--status`. Asserting presence alone
    would stay green through exactly that reordering.
    """
    skill = (CORE_SCRIPTS.parent.parent / ".claude" / "skills" / "worker-loop"
             / "SKILL.md").read_text(encoding="utf-8")

    invoke = skill.find('Skill(aspirations-verify)')
    assert invoke != -1, (
        "worker-loop Phase 4a must INVOKE the verify skill (guard-1867: never "
        "transcribe a sub-skill's steps)")
    line = skill[invoke:skill.find("\n", invoke)]
    assert 'scope="own-unit"' in line, (
        f"the invocation must be scoped to this Body's own unit, got: {line!r}")

    close = skill.find("iteration-close.sh --phase verify")
    assert close != -1, "the mechanical close writer call is missing from Phase 4a"
    assert invoke < close, (
        "the scoped verify must run BEFORE the mechanical close, not after it")

    assert we.LIFECYCLE_DISPOSITIONS["verify-own-unit"].pending_goal is None, (
        "the wiring has landed, so the row must no longer read as pending")


def test_worker_skips_all_reducer_only_phases():
    # The whole point of a worker: encode/reflect/consolidate stay reducer-only.
    for p in ("verify", "spark", "complete-review", "state-update",
              "evolution", "learning-gate", "productivity-check"):
        assert not we.worker_should_run_phase(p), f"worker must SKIP {p}"
        assert p in we.REDUCER_ONLY_PHASES


def test_reducer_only_and_worker_phases_disjoint():
    # A phase is never both run-by-worker AND reducer-only.
    assert set(we.WORKER_PHASES).isdisjoint(we.REDUCER_ONLY_PHASES)


def test_unknown_phase_not_run_by_worker():
    # Conservative: a worker never runs a phase not explicitly granted to it.
    assert not we.worker_should_run_phase("decompose")
    assert not we.worker_should_run_phase("precheck")
    assert not we.worker_should_run_phase("nonsense")


# --------- WM routing (activation signal: forked body-WM-file exists) ---------

def _mk_agent(tmp_path: Path, name: str = "alpha") -> Path:
    (tmp_path / "agents" / name / "session").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _fork_body(tmp_path: Path, sid: str, name: str = "alpha") -> Path:
    bsd = tmp_path / "agents" / name / "sessions" / sid
    bsd.mkdir(parents=True, exist_ok=True)
    (bsd / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    return bsd


def test_wm_path_agent_wide_without_unit_key(tmp_path):
    _mk_agent(tmp_path)
    p = we.worker_wm_path("alpha", None, project_root=tmp_path)
    assert p == tmp_path / "agents" / "alpha" / "session" / "working-memory.yaml"


def test_wm_path_agent_wide_when_no_forked_body(tmp_path):
    # unit_key given but the Body never forked a WM file (reducer/observer) ->
    # agent-wide. This is the dormant single-runner collapse.
    _mk_agent(tmp_path)
    p = we.worker_wm_path("alpha", SID_A, project_root=tmp_path)
    assert p == tmp_path / "agents" / "alpha" / "session" / "working-memory.yaml"


def test_wm_path_routes_to_body_when_forked(tmp_path):
    _mk_agent(tmp_path)
    _fork_body(tmp_path, SID_A)
    p = we.worker_wm_path("alpha", SID_A, project_root=tmp_path)
    assert p == tmp_path / "agents" / "alpha" / "sessions" / SID_A / "working-memory.yaml"


def test_two_forked_bodies_get_distinct_wm_paths(tmp_path):
    # The isolation the worker path needs: each forked Body writes its OWN WM.
    _mk_agent(tmp_path)
    _fork_body(tmp_path, SID_A)
    _fork_body(tmp_path, SID_B)
    pa = we.worker_wm_path("alpha", SID_A, project_root=tmp_path)
    pb = we.worker_wm_path("alpha", SID_B, project_root=tmp_path)
    assert pa != pb
    assert pa.parent.name == SID_A and pb.parent.name == SID_B


# ----------------------- source gate () -----------------------
#
# An agent-queue goal (source='agent') is claimable ONLY on the box holding
# that agent's DDB runner claim; the claim endpoint refuses every other box
# no_claim (STRUCTURAL). The gate keys on CLAIM-HOLDING, not role: a worker
# co-resident on the claim box CAN claim (). The probe is
# `owncloud_sync._owned_agents_with_provenance` (the ONE ownership
# implementation), provenance-gated: only 'live-claims' may answer a
# structural refusal; 'local-backend' falls through (no claim store exists
# there); unreadable provenances answer undetermined ('s direction).
#
# Every case below stubs the probe or the ownership function — no daemon, no
# DDB — so the file stays daemon-safe.


def _fake_owncloud_sync(monkeypatch, owned, provenance, exc=None):
    """Install a stub owncloud_sync whose ownership probe returns the given
    (owned, provenance) — or raises exc — under sys.modules."""
    import types
    mod = types.ModuleType("owncloud_sync")
    if exc is not None:
        def _boom(be=None):
            raise exc
        mod._owned_agents_with_provenance = _boom
    else:
        mod._owned_agents_with_provenance = lambda be=None: (owned, provenance)
    monkeypatch.setitem(sys.modules, "owncloud_sync", mod)
    return mod


# --- _agent_queue_claim_probe: provenance -> (held, provenance) mapping ----

def test_probe_local_backend_holds(monkeypatch):
    # No claim store exists off own-cloud: every queue is claimable on the box.
    _fake_owncloud_sync(monkeypatch, None, "local-backend")
    assert we._agent_queue_claim_probe("alpha") == (True, "local-backend")


def test_probe_live_claims_holds_named_agent(monkeypatch):
    _fake_owncloud_sync(monkeypatch, {"alpha", "bravo"}, "live-claims")
    assert we._agent_queue_claim_probe("alpha") == (True, "live-claims")


def test_probe_live_claims_holds_other_agent(monkeypatch):
    # The claim is held, but for a DIFFERENT agent's queue.
    _fake_owncloud_sync(monkeypatch, {"bravo"}, "live-claims")
    assert we._agent_queue_claim_probe("alpha") == (False, "live-claims")


def test_probe_live_claims_none(monkeypatch):
    _fake_owncloud_sync(monkeypatch, set(), "live-claims")
    assert we._agent_queue_claim_probe("alpha") == (False, "live-claims")


def test_probe_unknown_machine_cannot_tell(monkeypatch):
    # The box cannot prove what it holds: held=None, never a structural claim.
    _fake_owncloud_sync(monkeypatch, set(), "unknown-machine")
    held, prov = we._agent_queue_claim_probe("alpha")
    assert held is None and prov == "unknown-machine"


def test_probe_transient_error_cannot_tell(monkeypatch):
    _fake_owncloud_sync(monkeypatch, set(), "transient-error")
    held, prov = we._agent_queue_claim_probe("alpha")
    assert held is None and prov == "transient-error"


def test_probe_exception_cannot_tell(monkeypatch):
    # A persistent permission gap (or any failure) degrades to "cannot tell",
    # never to "owns nothing".
    _fake_owncloud_sync(monkeypatch, None, None,
                        exc=type("BoomError", (Exception,), {})("IAM"))
    held, prov = we._agent_queue_claim_probe("alpha")
    assert held is None and prov.startswith("probe-error:")


# --- goal_eligibility: the source gate verdicts ----------------------------

def test_source_agent_no_claim_is_reducer_only(monkeypatch):
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (False, "live-claims"))
    v = we.goal_eligibility(None, None, source="agent", agent="alpha")
    assert not v.eligible and not v.undetermined
    assert we._verdict_word(v) == "reducer-only"
    # The reason names the STRUCTURAL refusal so a silent skip cannot rot.
    assert "no_claim" in v.reason and "alpha" in v.reason
    assert "STRUCTURAL" in v.reason


def test_source_agent_claim_holding_is_eligible(monkeypatch):
    # OUTCOME 1 positive control: the SAME goal answers eligible on the
    # claim-holding box. The skill-less goal's own bridge verdict is
    # undetermined, but the gate's job is claimability — and it may not
    # invent a refusal where the claim endpoint would accept.
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (True, "live-claims"))
    v = we.goal_eligibility(None, None, source="agent", agent="alpha")
    assert v.eligible
    assert we._verdict_word(v) == "eligible"
    assert "HOLDS the live runner claim" in v.reason
    assert "alpha" in v.reason


def test_source_agent_probe_unreadable_is_undetermined(monkeypatch):
    # The refusal asserts a STRUCTURAL impossibility; on an unreadable claim
    # table it must NOT be asserted ('s direction).
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (None, "transient-error"))
    v = we.goal_eligibility(None, None, source="agent", agent="alpha")
    assert v.eligible and v.undetermined
    assert we._verdict_word(v) == "undetermined"
    assert "CANNOT tell" in v.reason
    assert "STRUCTURAL" not in v.reason or "NOT asserted" in v.reason


def test_source_agent_role_reducer_still_refuses(monkeypatch):
    # Holding the claim does NOT unlock a goal that declares itself
    # reducer-only: the source gate answers claimability, the role field
    # answers ownership, and the two compose (refusal wins).
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (True, "live-claims"))
    v = we.goal_eligibility(None, "reducer", source="agent", agent="alpha")
    assert not v.eligible
    assert we._verdict_word(v) == "reducer-only"
    assert "executable_by_role='reducer'" in v.reason


def test_source_unset_keeps_legacy_verdict(monkeypatch):
    # No source field: the gate must not fire at all (the existing corpus is
    # read without it in every pre-existing call shape).
    def _refuse_probe(agent):
        raise AssertionError("probe must not run without a source field")
    monkeypatch.setattr(we, "_agent_queue_claim_probe", _refuse_probe)
    v = we.goal_eligibility(None, None)
    assert v.eligible and v.undetermined
    assert we._verdict_word(v) == "undetermined"


def test_source_world_does_not_fire_gate(monkeypatch):
    def _refuse_probe(agent):
        raise AssertionError("probe must not run for source='world'")
    monkeypatch.setattr(we, "_agent_queue_claim_probe", _refuse_probe)
    v = we.goal_eligibility(None, None, source="world", agent="alpha")
    assert v.eligible and v.undetermined


def test_source_cross_agent_does_not_fire_gate(monkeypatch):
    # 'cross-agent:<owner>' is the peer-queue variant (, a separate
    # goal), not this worker's own queue: the gate answers as before.
    def _refuse_probe(agent):
        raise AssertionError("probe must not run for a cross-agent row")
    monkeypatch.setattr(we, "_agent_queue_claim_probe", _refuse_probe)
    v = we.goal_eligibility(None, None, source="cross-agent:zeta")
    assert v.eligible and v.undetermined


def test_source_agent_agent_defaults_to_env(monkeypatch):
    # The worker judging its OWN pool passes no --agent: the probe must key on
    # the session's MIND_AGENT.
    seen = {}

    def _probe(agent):
        seen["agent"] = agent
        return (False, "live-claims")

    monkeypatch.setattr(we, "_agent_queue_claim_probe", _probe)
    monkeypatch.setenv("MIND_AGENT", "zeta")
    v = we.goal_eligibility(None, None, source="agent")
    assert not v.eligible
    assert seen["agent"] == "zeta"
    assert "zeta" in v.reason


def test_source_agent_explicit_agent_overrides_env(monkeypatch):
    seen = {}

    def _probe(agent):
        seen["agent"] = agent
        return (True, "live-claims")

    monkeypatch.setattr(we, "_agent_queue_claim_probe", _probe)
    monkeypatch.setenv("MIND_AGENT", "zeta")
    v = we.goal_eligibility(None, None, source="agent", agent="echo")
    assert v.eligible
    assert seen["agent"] == "echo"


# --- CLI wiring: the flags reach the gate (not swallowed by REMAINDER) -----

def test_cli_goal_eligible_source_agent_no_claim_refuses(monkeypatch):
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (False, "live-claims"))
    rc = we._main(["goal-eligible", "--source", "agent",
                   "--agent", "alpha"])
    assert rc == 1


def test_cli_goal_eligible_source_agent_claim_holding_eligible(monkeypatch):
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (True, "live-claims"))
    rc = we._main(["goal-eligible", "--source", "agent",
                   "--agent", "alpha"])
    assert rc == 0


def test_cli_goal_eligible_flags_before_skill_remainder(monkeypatch):
    # guard-920 shape: the skill field verbatim, args and all, AFTER the
    # flags — a trailing flag would be swallowed as skill text.
    monkeypatch.setattr(we, "_agent_queue_claim_probe",
                        lambda agent: (False, "live-claims"))
    rc = we._main(["goal-eligible", "--role", "worker",
                   "--source", "agent", "--agent", "alpha",
                   "/tree add"])
    assert rc == 1  # source gate refuses regardless of the role/skill half


def test_cli_goal_eligible_no_source_legacy_behaviour(monkeypatch):
    # Pre-existing call shape (no --source/--agent) is byte-for-byte unchanged:
    # skill-less goal -> undetermined, rc 0.
    def _refuse_probe(agent):
        raise AssertionError("probe must not run without --source agent")
    monkeypatch.setattr(we, "_agent_queue_claim_probe", _refuse_probe)
    rc = we._main(["goal-eligible"])
    assert rc == 0


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
