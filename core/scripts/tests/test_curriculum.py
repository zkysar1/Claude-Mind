"""Tests for curriculum.py cross-queue graduation counting ( / ).

g-115-1560 added cross_queue counting to the curriculum count_check gate in BOTH
core/scripts/curriculum.py (CLI) and mind_api/src/endpoints/curriculum.py (daemon)
so a lane-specialized agent that works the shared WORLD queue accumulates
graduation credit instead of staying pinned at cur-01 forever. That close left no
test file (verified 2026-06-19); g-115-1563 adds this coverage plus a CLI/daemon
parity guard for the guard-742 daemon-mirror drift class.

Path-override approach: curriculum.py reads module globals AGENT_DIR / WORLD_DIR /
AGENT_NAME at call time, so the tests monkeypatch those attributes directly (the
conftest-documented pattern for path-dependent modules). conftest pre-locks
AGENT_DIR so the import-time assert_agent_dir("curriculum") passes.
"""
import json
import sys
from pathlib import Path

# core/scripts on path (conftest also inserts this; explicit for `py -3` direct runs).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import curriculum  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "curriculum.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "curriculum.py"


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# --- count_world_attributed ------------------------------------------------

def test_count_world_attributed_counts_only_self_completed(tmp_path, monkeypatch):
    world = tmp_path / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        {"goals": [
            {"id": "w-1", "status": "completed", "completed_by": "delta"},
            {"id": "w-2", "status": "completed", "completed_by": "delta"},
            {"id": "w-3", "status": "pending", "completed_by": "delta"},    # status mismatch
            {"id": "w-4", "status": "completed"},                            # no completed_by
        ]},
        {"goals": [
            {"id": "w-5", "status": "completed", "completed_by": "delta"},
        ]},
    ])
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    # w-1, w-2, w-5 are completed AND completed_by==delta -> 3
    assert curriculum.count_world_attributed("goals[*].status", "completed", "delta") == 3


def test_count_world_attributed_excludes_other_agents(tmp_path, monkeypatch):
    world = tmp_path / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        {"goals": [
            {"id": "w-1", "status": "completed", "completed_by": "delta"},
            {"id": "w-2", "status": "completed", "completed_by": "alpha"},
            {"id": "w-3", "status": "completed", "completed_by": "zeta"},
        ]},
    ])
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    assert curriculum.count_world_attributed("goals[*].status", "completed", "delta") == 1
    assert curriculum.count_world_attributed("goals[*].status", "completed", "alpha") == 1
    assert curriculum.count_world_attributed("goals[*].status", "completed", "bravo") == 0


def test_count_world_attributed_non_flatten_field_returns_zero(tmp_path, monkeypatch):
    world = tmp_path / "world"
    _write_jsonl(world / "aspirations.jsonl", [
        {"status": "completed", "completed_by": "delta"},
    ])
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    # Non-flatten field (no "[*].") is graduation-completion-scoped -> always 0
    assert curriculum.count_world_attributed("status", "completed", "delta") == 0


# --- evaluate_gate count_check cross_queue ---------------------------------

def _setup_gate_fixtures(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    world = tmp_path / "world"
    _write_jsonl(agent / "aspirations.jsonl", [
        {"goals": [
            {"id": "a-1", "status": "completed"},
            {"id": "a-2", "status": "completed"},
            {"id": "a-3", "status": "pending"},
        ]},
    ])
    _write_jsonl(world / "aspirations.jsonl", [
        {"goals": [
            {"id": "w-1", "status": "completed", "completed_by": "delta"},
            {"id": "w-2", "status": "completed", "completed_by": "delta"},
            {"id": "w-3", "status": "completed", "completed_by": "delta"},
            {"id": "w-4", "status": "completed", "completed_by": "alpha"},   # other agent
        ]},
    ])
    monkeypatch.setattr(curriculum, "AGENT_DIR", agent)
    monkeypatch.setattr(curriculum, "WORLD_DIR", world)
    monkeypatch.setattr(curriculum, "AGENT_NAME", "delta")


def test_count_check_without_cross_queue_is_agent_only(tmp_path, monkeypatch):
    _setup_gate_fixtures(tmp_path, monkeypatch)
    gate = {"type": "count_check", "file": "aspirations.jsonl",
            "field": "goals[*].status", "value": "completed",
            "operator": ">=", "threshold": 3}
    # No cross_queue flag: agent-queue only = 2 completed -> 2 < 3 -> not passed.
    # Regression guard: existing count_check gates (no flag) MUST stay agent-only.
    passed, value = curriculum.evaluate_gate(gate)
    assert value == 2
    assert passed is False


def test_count_check_with_cross_queue_adds_self_attributed_world(tmp_path, monkeypatch):
    _setup_gate_fixtures(tmp_path, monkeypatch)
    gate = {"type": "count_check", "file": "aspirations.jsonl",
            "field": "goals[*].status", "value": "completed",
            "operator": ">=", "threshold": 3, "cross_queue": True}
    # 2 agent + 3 world-attributed-to-delta (w-4/alpha excluded) = 5 -> 5 >= 3 -> passed.
    passed, value = curriculum.evaluate_gate(gate)
    assert value == 5
    assert passed is True


# --- CLI/daemon parity guard (guard-742 daemon-mirror drift class) ----------

def test_cli_daemon_cross_queue_parity():
    """Both curriculum implementations MUST carry the cross_queue path in sync.

    g-115-1560 patched the CLI (core/scripts/curriculum.py) and the daemon
    reimplementation (mind_api/src/endpoints/curriculum.py) as byte-parallel
    copies. A fix to only one side is half a fix (guard-742). This guard fails if
    either side loses its cross-queue counting function, the cross_queue gate
    branch, or completed_by attribution.
    """
    assert CLI_FILE.is_file(), f"CLI curriculum missing: {CLI_FILE}"
    assert DAEMON_FILE.is_file(), f"daemon curriculum missing: {DAEMON_FILE}"
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")

    # cross-queue counting function present on both sides (CLI public, daemon private)
    assert "def count_world_attributed(" in cli
    assert "def _count_world_attributed(" in daemon
    # the count_check cross_queue branch present on both sides
    assert 'gate.get("cross_queue")' in cli
    assert 'gate.get("cross_queue")' in daemon
    # completed_by attribution discipline present on both sides (NOT claimed_by)
    assert "completed_by" in cli
    assert "completed_by" in daemon


# --- evaluate() response-SHAPE contract () ------------------------
#
# evaluate has FOUR return shapes and only `gates` appears in every non-error
# one. The terminal early-return (curriculum.py:393) omits stage_name,
# gates_total, gates_passed_count and next_stage, which the aspirations-precheck
# curriculum dispatch used to read unconditionally — rendering
# "re-evaluated None — None/None gates pass (not yet promotable)" for an agent at
# the END of its curriculum, i.e. asserting the opposite of the truth.
#
# The premise these tests correct: the four names were reported as "fields that
# do not exist". They DO exist (curriculum.py:440-448); they are BRANCH-LOCAL.
# The bug is runtime shape variance, not a stale name — which is why a static
# name-existence scanner reports clean on it and cannot be extended to catch it.

def _write_curriculum(path, gates):
    """One-stage-plus-successor curriculum; `gates` becomes stage-1's gate list."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "current_stage": "cur-01",
        "stages": [
            {"id": "cur-01", "name": "First", "graduation_gates": gates},
            {"id": "cur-02", "name": "Second", "graduation_gates": []},
        ],
    }), encoding="utf-8")


def _run_evaluate(monkeypatch, tmp_path, gates, gate_verdicts):
    """Call cmd_evaluate against a tmp curriculum; return the parsed JSON."""
    import io
    import contextlib
    cur_path = tmp_path / "curriculum.yaml"
    _write_curriculum(cur_path, gates)
    monkeypatch.setattr(curriculum, "CURRICULUM_PATH", cur_path)
    # Scripted gate verdicts keep this a SHAPE test, not a gate-semantics test.
    verdicts = list(gate_verdicts)
    monkeypatch.setattr(curriculum, "evaluate_gate",
                        lambda g: (verdicts.pop(0), 1.0))
    monkeypatch.setattr(curriculum, "refresh_competence_for_gates",
                        lambda *a, **k: None)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        curriculum.cmd_evaluate(None)
    return json.loads(buf.getvalue())


def test_evaluate_terminal_shape_omits_the_four_scalars(tmp_path, monkeypatch):
    """Terminal stage: terminal_stage=True, and the four scalars are ABSENT.

    This pins the exact absence the precheck dispatch now branches on. If the
    endpoint ever starts emitting these on the terminal path, the branch's
    premise changed and this test says so.
    """
    out = _run_evaluate(monkeypatch, tmp_path, gates=[], gate_verdicts=[])

    assert out["terminal_stage"] is True
    assert out["all_passed"] is True
    assert out["gates"] == []
    assert out["current_stage"] == "cur-01"
    for absent in ("stage_name", "gates_total", "gates_passed_count", "next_stage"):
        assert absent not in out, (
            f"{absent!r} present on the terminal shape — the precheck dispatch's "
            "terminal branch was written against its absence")


def test_evaluate_non_terminal_counts_derivable_from_gates(tmp_path, monkeypatch):
    """NON-terminal fixture (guard-1220): the ratio can come out wrong here.

    A terminal-only fixture cannot falsify the count fix — all_passed is true and
    gates is empty whichever way the counts are computed. With 3 gates at 2-pass/
    1-fail, deriving from `gates` must yield exactly 2/3: a fix that mis-derives
    reads 3/3, 0/0 or None/None and this fails.
    """
    gates = [{"id": f"g{i}", "type": "count_check"} for i in range(3)]
    out = _run_evaluate(monkeypatch, tmp_path, gates=gates,
                        gate_verdicts=[True, True, False])

    # The derivation the precheck dispatch now uses, against a ratio that is
    # neither all-pass nor all-fail.
    derived_total = len(out["gates"])
    derived_passed = sum(1 for g in out["gates"] if g["passed"])
    assert (derived_passed, derived_total) == (2, 3)

    # ...and it agrees with the branch-local scalars on the shape that has them,
    # so deriving from `gates` is a strictly safer read, not a different answer.
    assert out["gates_total"] == derived_total
    assert out["gates_passed_count"] == derived_passed
    assert out["all_passed"] is False
    assert out["next_stage"] == "cur-02"
    assert out["stage_name"] == "First"
    assert "terminal_stage" not in out


def test_terminal_shape_parity_cli_vs_daemon():
    """The two shape tests above exercise the CLI cmd_evaluate — but the precheck
    dispatch calls curriculum-evaluate.sh, which is DAEMON-ONLY. So production
    runs mind_api/src/endpoints/curriculum.py, and a daemon-side drift would
    leave those tests green while the terminal branch breaks. Same guard-742
    daemon-mirror class the cross-queue parity test above covers; asserted on
    source text because only one of the two copies is importable here.
    """
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    for side, src in (("CLI", cli), ("daemon", daemon)):
        # the terminal early-return exists on both sides...
        assert '"terminal_stage": True,' in src, f"{side} lost its terminal_stage early return"
        # ...and the four scalars stay OUT of it: locate the terminal return
        # block and assert none of them appear between its braces.
        i = src.index('"terminal_stage": True,')
        block = src[src.rindex("{", 0, i):src.index("}", i)]
        for scalar in ("stage_name", "gates_total", "gates_passed_count", "next_stage"):
            assert scalar not in block, (
                f"{side} terminal return now emits {scalar!r} — the precheck "
                "terminal branch was written against its absence; re-check both")


def test_precheck_dispatch_branches_on_terminal_stage():
    """The consuming pseudocode must carry the terminal branch ().

    Prose is not type-checked; this is the only thing that pins it. Same shape as
    the CLI/daemon parity guard above — an assertion on source text, because the
    artifact IS text.
    """
    skill = (PROJECT_ROOT / ".claude" / "skills" / "aspirations-precheck"
             / "SKILL.md").read_text(encoding="utf-8")
    assert "IF eval_json.terminal_stage == true:" in skill, (
        "precheck curriculum dispatch lost its terminal_stage branch — a "
        "terminal-stage agent will render 'None/None gates pass (not yet "
        "promotable)' again")
    # Counts derived from `gates`, not read from the branch-local scalars.
    assert "gates_total        = len(eval_json.gates)" in skill
    assert "count of eval_json.gates where passed == true" in skill
