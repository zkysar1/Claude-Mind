"""Tests for _competence.py — the competence-assessment SSOT ().

Covers the assess() formula, the refresh-before-evaluate glue
(refresh_competence_for_gates), write-side field preservation, and the
CLI/daemon wiring parity (sibling to test_curriculum.py's
test_cli_daemon_cross_queue_parity — guard-742 half-fix class).
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _competence  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_FILE = PROJECT_ROOT / "core" / "scripts" / "curriculum.py"
DAEMON_FILE = PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "curriculum.py"
WRAPPER_FILE = PROJECT_ROOT / "core" / "scripts" / "competence-assess.py"

METRIC_GATE = {
    "type": "metric_threshold",
    "metric": _competence.COMPETENCE_METRIC,
    "operator": ">=",
    "threshold": 0.25,
}


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _seed_world(world: Path, tree_nodes=2, resolved=1, active=2, rb=4, guards=2, completed=3):
    tree = {"nodes": {f"n{i}": {"file": f"n{i}.md"} for i in range(tree_nodes)}}
    tree_path = world / "knowledge" / "tree" / "_tree.yaml"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(yaml.safe_dump(tree), encoding="utf-8")
    _write_jsonl(world / "pipeline.jsonl",
                 [{"stage": "resolved"}] * resolved + [{"stage": "active"}] * active)
    _write_jsonl(world / "reasoning-bank.jsonl", [{"status": "active"}] * rb)
    _write_jsonl(world / "guardrails.jsonl", [{"status": "active"}] * guards)
    _write_jsonl(world / "aspirations.jsonl",
                 [{"goals": [{"status": "completed"}] * completed}])


# --- assess() formula -------------------------------------------------------

def test_assess_formula_components(tmp_path):
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    _seed_world(world, tree_nodes=2, resolved=1, active=2, rb=4, guards=2, completed=3)
    result = _competence.assess(world, agent)
    c = result["components"]
    assert c["knowledge_density"] == round(2 / 50, 4)
    assert c["pipeline_activity"] == round((1 + 0.5 * 2) / 5, 4)
    assert c["encoded_lessons"] == round((4 + 0.5 * 2) / 20, 4)
    assert c["completion_breadth"] == round(3 / 25, 4)
    expected = round((2 / 50 + 2 / 5 + 5 / 20 + 3 / 25) / 4, 4)
    assert result["average_competence"] == expected


def test_assess_caps_each_component_at_one(tmp_path):
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    _seed_world(world, tree_nodes=500, resolved=50, active=50, rb=100, guards=50, completed=100)
    result = _competence.assess(world, agent)
    assert result["average_competence"] == 1.0
    assert all(v == 1.0 for v in result["components"].values())


def test_assess_empty_world_is_zero(tmp_path):
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    world.mkdir()
    agent.mkdir()
    result = _competence.assess(world, agent)
    assert result["average_competence"] == 0.0


# --- write_developmental_stage ----------------------------------------------

def test_write_preserves_evolve_owned_fields(tmp_path):
    """The refresh only owns current_assessment.{avg,assessed_at,components,
    evidence,producer} — overall_stage / exploration / other current_assessment
    keys written by aspirations-evolve Step 0 must survive untouched."""
    agent = tmp_path / "agent"
    agent.mkdir()
    stage_path = agent / "developmental-stage.yaml"
    stage_path.write_text(yaml.safe_dump({
        "overall_stage": "applying",
        "current_assessment": {
            "stage": "applying",
            "average_competence": 0.6,
            "highest_capability": "EXPLOIT",
        },
        "exploration": {"epsilon": 0.4},
    }, sort_keys=False), encoding="utf-8")

    result = {"average_competence": 0.9, "components": {"k": 1}, "evidence": {"e": 1}}
    _competence.write_developmental_stage(agent, result)

    doc = yaml.safe_load(stage_path.read_text(encoding="utf-8"))
    assert doc["overall_stage"] == "applying"
    assert doc["exploration"] == {"epsilon": 0.4}
    ca = doc["current_assessment"]
    assert ca["average_competence"] == 0.9
    assert ca["assessed_at"]  # provenance stamped
    assert ca["producer"] == _competence.PRODUCER
    assert ca["stage"] == "applying"              # evolve-owned key preserved
    assert ca["highest_capability"] == "EXPLOIT"  # evolve-owned key preserved


# --- assess_stage ( — script-enforced stage producer) ---------------

def _seed_tree_with_levels(world: Path, nodes: dict):
    """nodes: key -> (parent, depth, capability_level)."""
    tree = {"nodes": {
        k: {"file": f"{k}.md", "parent": p, "depth": d, "capability_level": lvl}
        for k, (p, d, lvl) in nodes.items()
    }}
    tree_path = world / "knowledge" / "tree" / "_tree.yaml"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(yaml.safe_dump(tree), encoding="utf-8")


def test_assess_stage_leaf_mean_and_bands(tmp_path):
    """tree_maturity = mean mapped capability_level of depth>=2 LEAVES; a node
    that is another node's parent is NOT a leaf; depth<2 excluded."""
    world = tmp_path / "world"
    _seed_tree_with_levels(world, {
        "branch": (None, 2, "MASTER"),          # parent of leaf-a → NOT a leaf
        "leaf-a": ("branch", 3, "EXPLOIT"),     # 0.70
        "leaf-b": (None, 2, "CALIBRATE"),       # 0.45
        "shallow": (None, 1, "MASTER"),         # depth<2 → excluded
    })
    sa = _competence.assess_stage(world)
    assert sa["leaves_counted"] == 2
    assert sa["tree_maturity"] == round((0.70 + 0.45) / 2, 4)  # 0.575
    assert sa["stage"] == "applying"            # 0.55 <= 0.575 < 0.80
    assert sa["highest_capability"] == "EXPLOIT"
    assert sa["lowest_capability"] == "CALIBRATE"
    assert sa["exploration_budget"] == round(1.0 - 0.575, 4)


def test_assess_stage_unmapped_levels_skipped(tmp_path):
    """Levels absent from COMPETENCE_MAPPING (e.g. REFERENCE) are skipped from
    the mean, not treated as zero."""
    world = tmp_path / "world"
    _seed_tree_with_levels(world, {
        "leaf-a": (None, 2, "EXPLOIT"),     # 0.70
        "leaf-r": (None, 2, "REFERENCE"),   # unmapped → skipped
    })
    sa = _competence.assess_stage(world)
    assert sa["leaves_counted"] == 1
    assert sa["unmapped_skipped"] == 1
    assert sa["tree_maturity"] == 0.70
    assert sa["stage"] == "applying"


def test_assess_stage_empty_tree_is_initial_state(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    sa = _competence.assess_stage(world)
    assert sa["tree_maturity"] == 0.0
    assert sa["stage"] == "exploring"
    assert sa["exploration_budget"] == 0.85
    assert sa["highest_capability"] == "EXPLORE"
    assert sa["lowest_capability"] == "EXPLORE"


def test_assess_stage_budget_clamped_at_floor(tmp_path):
    world = tmp_path / "world"
    _seed_tree_with_levels(world, {"leaf-a": (None, 2, "MASTER")})  # 0.90
    sa = _competence.assess_stage(world)
    assert sa["stage"] == "mastering"
    assert sa["exploration_budget"] == 0.15  # clamp floor (1-0.9=0.1 < 0.15)


def test_write_with_stage_assessment_owns_full_block(tmp_path):
    """A full assess() result (carrying stage_assessment) writes the stage
    block: overall_stage + ca.{stage,tree_maturity,highest,lowest,budget,
    resolved_hypotheses(=pipeline_resolved)} + exploration.epsilon —
    schema_operations preserved; the producer stamp is honest (g-115-2624)."""
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    _seed_world(world, resolved=7)
    _seed_tree_with_levels(world, {"leaf-a": (None, 2, "EXPLOIT")})
    stage_path = agent / "developmental-stage.yaml"
    stage_path.write_text(yaml.safe_dump({
        "overall_stage": "exploring",
        "current_assessment": {"stage": "exploring", "resolved_hypotheses": 0},
        "schema_operations": {"equilibration_state": "balanced", "log": [{"op": "x"}]},
        "exploration": {"epsilon": 0.85, "category_allocation": {"a": 1}},
    }, sort_keys=False), encoding="utf-8")

    result = _competence.assess(world, agent)
    _competence.write_developmental_stage(agent, result)

    doc = yaml.safe_load(stage_path.read_text(encoding="utf-8"))
    ca = doc["current_assessment"]
    assert doc["overall_stage"] == "applying"          # 0.70 tree_maturity band
    assert ca["stage"] == "applying"
    assert ca["tree_maturity"] == 0.70
    assert ca["highest_capability"] == "EXPLOIT"
    assert ca["lowest_capability"] == "EXPLOIT"
    assert ca["exploration_budget"] == round(1.0 - 0.70, 4)
    assert ca["resolved_hypotheses"] == 7              # deduped to pipeline_resolved
    assert ca["resolved_hypotheses"] == ca["evidence"]["pipeline_resolved"]
    assert doc["exploration"]["epsilon"] == round(1.0 - 0.70, 4)
    assert doc["exploration"]["category_allocation"] == {"a": 1}   # preserved
    assert doc["schema_operations"]["log"] == [{"op": "x"}]        # preserved


# --- refresh_competence_for_gates --------------------------------------------

def test_refresh_skipped_when_no_metric_gate(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    gates = [{"type": "count_check", "file": "aspirations.jsonl",
              "field": "goals[*].status", "value": "completed",
              "operator": ">=", "threshold": 3}]
    status = _competence.refresh_competence_for_gates(gates, tmp_path / "world", agent)
    assert status == "skipped"
    assert not (agent / "developmental-stage.yaml").exists()


def test_refresh_ok_writes_metric_and_provenance(tmp_path):
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    _seed_world(world)
    status = _competence.refresh_competence_for_gates([METRIC_GATE], world, agent)
    assert status == "ok"
    doc = yaml.safe_load((agent / "developmental-stage.yaml").read_text(encoding="utf-8"))
    ca = doc["current_assessment"]
    assert ca["producer"] == _competence.PRODUCER
    assert ca["assessed_at"]
    assert isinstance(ca["average_competence"], float)


def test_refresh_fail_open_on_unreadable_world(tmp_path):
    """A refresh failure returns 'failed: ...' and never raises — gate
    evaluation must proceed with the stored value."""
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    agent.mkdir()
    # Poison the tree so yaml.safe_load raises inside assess().
    tree_path = world / "knowledge" / "tree" / "_tree.yaml"
    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text("{unclosed: [", encoding="utf-8")
    status = _competence.refresh_competence_for_gates([METRIC_GATE], world, agent)
    assert status.startswith("failed:")


def test_refresh_ignores_other_metric_dotpaths(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    gates = [{"type": "metric_threshold",
              "metric": "developmental-stage.exploration.epsilon",
              "operator": "<=", "threshold": 0.5}]
    status = _competence.refresh_competence_for_gates(gates, tmp_path / "world", agent)
    assert status == "skipped"


# --- wiring parity (guard-742 half-fix class) ---------------------------------

def test_cli_daemon_competence_refresh_parity():
    """Both curriculum evaluate implementations MUST carry the 
    refresh wiring in sync — a fix to only one side is half a fix."""
    cli = CLI_FILE.read_text(encoding="utf-8")
    daemon = DAEMON_FILE.read_text(encoding="utf-8")
    for src, name in ((cli, "CLI"), (daemon, "daemon")):
        assert "refresh_competence_for_gates(" in src, f"{name} lost the refresh call"
        assert '"competence_refresh"' in src, f"{name} lost the output key"


def test_wrapper_delegates_to_ssot():
    """competence-assess.py must import from _competence, not re-implement
    the formula (split-brain regression guard)."""
    wrapper = WRAPPER_FILE.read_text(encoding="utf-8")
    assert "from _competence import assess, write_developmental_stage" in wrapper
    assert "def assess(" not in wrapper
    assert "N_KNOWLEDGE =" not in wrapper


# --- daemon round-trip () -------------------------------------------

def test_daemon_roundtrip_evaluate_refreshes_metric(tmp_path):
    """Live integration path (): POST /v1/curriculum/evaluate with a
    competence metric gate → the daemon-side refresh fires BEFORE gate
    evaluation → developmental-stage.yaml gains assessed_at/producer, the
    response carries competence_refresh=ok, and the gate reads the RECOMPUTED
    value (not the stale seeded one). Uses the in-process DaemonFixture (tmp
    project root) — NOT the daemon_integration subprocess pattern."""
    import json as _json
    import urllib.request
    from _daemon_fixture import DaemonFixture

    world = tmp_path / "world"
    _seed_world(world)  # deterministic nonzero competence inputs

    with DaemonFixture(world, agent="alpha") as df:
        agent_dir = df.project_root / "agents" / "alpha"
        expected = _competence.assess(world, agent_dir)["average_competence"]
        assert expected != 0.99  # the stale seed must be distinguishable

        (agent_dir / "curriculum.yaml").write_text(yaml.safe_dump({
            "current_stage": "cur-01",
            "stages": [{
                "id": "cur-01", "name": "Test",
                "graduation_gates": [{
                    "type": "metric_threshold", "id": "gate_competence",
                    "metric": _competence.COMPETENCE_METRIC,
                    "operator": ">=", "threshold": 0.0,
                }],
            }],
        }, sort_keys=False), encoding="utf-8")
        (agent_dir / "developmental-stage.yaml").write_text(yaml.safe_dump({
            "overall_stage": "applying",
            "current_assessment": {"average_competence": 0.99},
        }, sort_keys=False), encoding="utf-8")

        req = urllib.request.Request(
            f"http://127.0.0.1:{df.port}/v1/curriculum/evaluate",
            data=b"", method="POST",
            headers={"X-Mind-Agent": "alpha"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200
            body = _json.loads(resp.read().decode("utf-8"))

        assert body["competence_refresh"] == "ok", f"refresh did not fire: {body}"
        gate = body["gates"][0]
        assert gate["current_value"] == expected, \
            f"gate read stale value: {gate} (expected recomputed {expected})"

        doc = yaml.safe_load((agent_dir / "developmental-stage.yaml").read_text(encoding="utf-8"))
        ca = doc["current_assessment"]
        assert ca["average_competence"] == expected
        assert ca["producer"] == _competence.PRODUCER
        assert ca["assessed_at"]
        # : the refresh now recomputes the stage block too (script is
        # the single producer). The seeded tree has no mappable depth>=2 leaves
        # → tree_maturity 0.0 → stage recomputed to "exploring" (stale seed
        # "applying" is corrected, not preserved).
        assert doc["overall_stage"] == "exploring"
        assert ca["stage"] == "exploring"
        assert ca["tree_maturity"] == 0.0
