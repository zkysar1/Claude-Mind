"""GATE-level tests for core/scripts/phase-4-26-gate.py.

The classifier this gate consumes is pinned by test_distinctive_tokens_discrimination.py;
the `retrieval_performed` discriminator is pinned by test_retrieval_performed_contract.py;
the all_unknown block is asserted by test_all_unknown_backstop.py — which pytest
collects ZERO tests from (it is a `main()`-style file, covered only by
run-invisible-suites.sh). So before g-115-3148 the gate's three OTHER refusal
branches, its exit codes, its override path and its telemetry were unpinned in
the pytest half entirely.

That matters more here than for an average gate. This one has already been
found non-functional twice for two DIFFERENT reasons — g-115-3113 (predicate
falsy-checked a key the real path never writes, 100% inert) and g-115-3134
(predicate saturated, would have passed ~92% of closes for a reason unrelated
to retrieval quality). A third instance is exactly what these tests exist to
make impossible, so the refusal assertions here are deliberately specific:
per guard-1082, a refusal test whose pass condition is `rc != 0` is equally
satisfied by a usage error or an unrelated crash, and resolves as a FALSE PASS.
Every CLI refusal below asserts the exact refusal reason AND is paired with a
control call on the same fixture that MUST succeed — if a control ever fails,
the harness is broken and every verdict in the run is void.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
_SCRIPT = CORE_SCRIPTS / "phase-4-26-gate.py"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CORE_SCRIPTS))
    spec.loader.exec_module(mod)
    return mod


gate = _load("phase_4_26_gate_g3148", "phase-4-26-gate.py")


def _session(goal_id="g-test-001", **over):
    """A session in the shape the REAL retrieve.sh path writes.

    Matches a live artifact measured 2026-08-01 (echo, g-115-4475):
    schema_version 3, goal_id set, 15 tree nodes + 45 supplementary items,
    utilization_method=infer, inference_stats.helpful=2, and NO
    `retrieval_performed` key — absent means performed (g-115-3126 contract).
    """
    d = {
        "schema_version": 3,
        "goal_id": goal_id,
        "tree_nodes_loaded": ["node-a", "node-b"],
        "supplementary_items": ["rb-001", "guard-001"],
        "utilization_pending": False,
        "utilization_method": "infer",
        "inference_stats": {"helpful": 2, "noise": 0, "unknown": 0},
    }
    d.update(over)
    return d


# ── Refusal branches: the three unpinned in the pytest half ────────────────


def test_infer_with_zero_helpful_blocks():
    """The branch the  classifier fix makes reachable.

    Before that fix the predicate was saturated, so helpful=0 was effectively
    unreachable on real manifests and this branch could not be exercised. It
    is reachable now (measured: own helpful/population fell 0.922 -> 0.085),
    which is what makes pinning it worth doing.
    """
    verdict, reason, method, helpful, path = gate._evaluate(
        _session(inference_stats={"helpful": 0, "noise": 4}), "g-test-001")
    assert verdict == "block"
    assert reason == "method=infer with helpful=0 — no positive signal"
    assert (method, helpful, path) == ("infer", 0, "infer-zero-helpful")


def test_all_noise_backstop_alone_blocks():
    verdict, reason, _m, _n, path = gate._evaluate(
        _session(utilization_method="all_noise"), "g-test-001")
    assert verdict == "block"
    assert "all_noise" in reason and "no positive signal" in reason
    assert path == "all-noise"


def test_utilization_pending_blocks():
    """Phase 4.26 never ran at all — no method recorded, pending still true."""
    verdict, reason, _m, _n, path = gate._evaluate(
        _session(utilization_method=None, utilization_pending=True),
        "g-test-001")
    assert verdict == "block"
    assert "utilization_pending=true" in reason
    assert path == "pending-true"


def test_all_unknown_backstop_alone_blocks():
    """Also asserted by test_all_unknown_backstop.py — which pytest collects
    zero tests from, so a pytest-only run proves nothing about it. One line
    here means the pytest half is not silently missing a refusal branch."""
    verdict, _r, _m, _n, path = gate._evaluate(
        _session(utilization_method="all_unknown"), "g-test-001")
    assert verdict == "block"
    assert path == "all-unknown"


# ── The passing side (guard-1790: verify BOTH directions, same run) ────────


def test_infer_with_positive_helpful_passes():
    """The live shape. If this ever blocks, the gate wedges every close."""
    verdict, _r, _m, helpful, path = gate._evaluate(_session(), "g-test-001")
    assert verdict == "pass"
    assert (helpful, path) == (2, "infer-helpful")


@pytest.mark.parametrize("method", ["manual", "all_helpful"])
def test_explicit_llm_classification_passes(method):
    verdict, _r, _m, _n, path = gate._evaluate(
        _session(utilization_method=method), "g-test-001")
    assert verdict == "pass"
    assert path == "explicit-classification"


def test_empty_population_is_not_a_firing():
    """Nothing was retrieved, so there is nothing to grade. This must map to
    `noop`, not `pass`: the retirement evaluator scores
    count(decision != 'noop'), so counting vacuous passes as firings would let
    an inert gate look busy — the precise way this gate hid for 3 weeks."""
    verdict, _r, _m, _n, path = gate._evaluate(
        _session(tree_nodes_loaded=[], supplementary_items=[]), "g-test-001")
    assert verdict == "pass"
    assert path == "empty-population"
    assert gate._DECISION_FOR_PATH[path] == "noop"


# ── Telemetry wiring (guard-502) ───────────────────────────────────────────


def test_every_branch_has_a_unique_decision_path():
    """Mutation guard. A branch that returns a path already used by another
    branch, or one absent from _DECISION_FOR_PATH, makes two different
    outcomes indistinguishable in meta/gate-firings.jsonl — which is the
    'silent path vs dead code' ambiguity guard-502 exists to prevent."""
    import re
    src = _SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("def _evaluate("):src.index("def _log_override(")]
    # The trailing element of every `return (...)` tuple in _evaluate.
    paths = re.findall(r',\s*"([a-z0-9-]+)"\)\s*$', body, flags=re.M)
    assert len(paths) >= 11, f"expected >=11 return branches, found {paths}"
    assert len(paths) == len(set(paths)), f"duplicate decision_path: {paths}"
    for p in paths:
        assert p in gate._DECISION_FOR_PATH, f"{p} missing from _DECISION_FOR_PATH"


def test_decision_values_are_all_in_the_gate_log_enum():
    from _gate_log import _VALID_DECISIONS
    for path, decision in gate._DECISION_FOR_PATH.items():
        assert decision in _VALID_DECISIONS, f"{path} -> invalid {decision!r}"


def test_gate_id_matches_gates_yaml():
    """_gate_log's contract: a gate_id absent from gates.yaml is invisible to
    the retirement evaluator, so the firings accumulate and nothing reads
    them."""
    root = CORE_SCRIPTS.parent.parent
    cfg = yaml.safe_load((root / "core" / "config" / "gates.yaml").read_text(
        encoding="utf-8"))
    ids = {g["id"] for g in cfg["gates"]}
    assert gate.GATE_ID in ids, (
        f"{gate.GATE_ID} not registered in core/config/gates.yaml — its "
        f"firings would be unreadable by gate-stats / gate-retirement-eval")


# ── CLI contract, end to end ───────────────────────────────────────────────


def _run(tmp_path, session, goal="g-cli-001", extra_args=(), meta=None):
    """Invoke the real script in a subprocess against a throwaway agent dir.

    MIND_AGENT_DIR / MIND_WORLD / MIND_META are the documented test seams
    in _paths.py; without them this would read and write the LIVE agent
    session and the LIVE override ledger. STORAGE_BACKEND=local is mandatory
    per guard-955 (under own-cloud a tmp write derives its S3 key from the
    env id, not the tmp dir, and collides with the production object).
    """
    adir = tmp_path / "agent" / "session"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "retrieval-session.json").write_text(
        json.dumps(session), encoding="utf-8")
    world = tmp_path / "world"
    world.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({
        "MIND_AGENT_DIR": str(tmp_path / "agent"),
        "MIND_WORLD": str(world),
        "MIND_META": str(meta or (tmp_path / "meta")),
        "STORAGE_BACKEND": "local",
    })
    (Path(env["MIND_META"])).mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(_SCRIPT), "--goal", goal,
                        *extra_args], capture_output=True, text=True, env=env)
    return r, world


def test_cli_blocks_with_rc1_and_names_the_reason(tmp_path):
    """guard-1082: assert the SPECIFIC refusal, not merely rc != 0 — a usage
    error and a crash both produce a nonzero rc and would pass a coarse check.
    The paired control below is the other half of that rule."""
    r, _ = _run(tmp_path, _session("g-cli-001",
                                   inference_stats={"helpful": 0, "noise": 4}))
    assert r.returncode == 1, f"expected block rc=1, got {r.returncode}: {r.stderr}"
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "block"
    assert payload["reason"] == "method=infer with helpful=0 — no positive signal"
    assert payload["override"] is False


def test_cli_control_call_must_succeed(tmp_path):
    """THE PAIRED CONTROL for the refusal test above (guard-1082). Same
    fixture, one field changed. If this fails, the harness is broken and the
    refusal test's verdict is void — do NOT weaken the gate to make it green."""
    r, _ = _run(tmp_path, _session("g-cli-001"))
    assert r.returncode == 0, f"control must pass, got {r.returncode}: {r.stderr}"
    assert json.loads(r.stdout)["verdict"] == "pass"


def test_cli_override_passes_and_writes_the_ledger(tmp_path):
    """The documented escape path. It must flip the verdict AND leave an audit
    row — an override that passes silently is indistinguishable from a gate
    that never fired."""
    r, world = _run(
        tmp_path, _session("g-cli-002", utilization_method="all_unknown"),
        goal="g-cli-002",
        extra_args=("--no-retrieval-applicable", "test justification"))
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["verdict"] == "pass"
    assert payload["override"] is True
    assert payload["override_reason"] == "test justification"
    assert "all_unknown" in payload["original_block_reason"], (
        "the original block reason must survive the override — without it the "
        "ledger cannot say WHAT was overridden")

    ledger = world / "phase-4-26-overrides.jsonl"
    assert ledger.is_file(), "override ledger not written"
    rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["goal_id"] == "g-cli-002"
    assert rows[0]["method"] == "all_unknown"
    assert rows[0]["reason"] == "test justification"


def test_cli_emits_a_firing_record(tmp_path, monkeypatch):
    """Without this the gate is enforcing but unobservable: until 
    the ONLY trace of any decision was the override ledger, which records
    overridden blocks and nothing else — so blocks that STOOD and every pass
    left no evidence at all, and the pass/block split was unmeasurable.

    GATE_LOG_ALLOW_PYTEST is the sanctioned opt-out (see _gate_log docstring);
    MIND_META redirects the write so nothing lands in the production store.
    """
    meta = tmp_path / "meta"
    # setenv (not a local dict) because _run() snapshots os.environ and the
    # subprocess inherits the flag from there. monkeypatch writes to the real
    # os.environ and restores at teardown even if an assertion below raises.
    monkeypatch.setenv("GATE_LOG_ALLOW_PYTEST", "1")
    r, _ = _run(tmp_path, _session("g-cli-003", inference_stats={"helpful": 0}),
                goal="g-cli-003", meta=meta)
    assert r.returncode == 1, r.stderr

    firings = meta / "gate-firings.jsonl"
    assert firings.is_file(), "no firing record written for a block decision"
    rows = [json.loads(l) for l in firings.read_text(encoding="utf-8").splitlines() if l.strip()]
    mine = [x for x in rows if x["gate_id"] == gate.GATE_ID]
    assert len(mine) == 1, f"expected exactly one firing, got {mine}"
    assert mine[0]["decision"] == "block"
    assert mine[0]["extra"]["decision_path"] == "infer-zero-helpful"
    assert mine[0]["extra"]["would_block"] is True
