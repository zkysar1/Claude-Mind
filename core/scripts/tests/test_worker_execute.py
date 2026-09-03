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


def test_verify_own_unit_is_marked_pending_until_the_loop_actually_invokes_it():
    """The phase is DECLARED but worker-loop does not yet invoke the verify
    skill, so the row must carry `pending_goal` — otherwise it reads downstream
    as evidence the wiring exists.

    This test is deliberately written to FAIL when the wiring lands: whoever
    wires worker-loop Phase 4a to invoke the scoped verify must come here,
    delete the pending marker, and delete this test. That is the point — a
    pending marker nobody is forced to remove becomes permanent, and then the
    table is lying in the other direction.
    """
    d = we.LIFECYCLE_DISPOSITIONS["verify-own-unit"]
    assert d.pending_goal == "g-306-417", (
        "verify-own-unit must stay marked pending until worker-loop Phase 4a "
        "actually invokes the scoped verify skill; if you just wired it, "
        "remove pending_goal AND delete this test")


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


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
