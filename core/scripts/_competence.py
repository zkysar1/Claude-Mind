"""Competence assessment SSOT — shared by CLI and daemon (6).

Single source of truth for computing and writing the
`developmental-stage.current_assessment.average_competence` metric consumed
by curriculum `metric_threshold` gates. Extracted from
`core/scripts/competence-assess.py` (which now thin-wraps this module) so
both curriculum evaluate implementations — `core/scripts/curriculum.py`
`cmd_evaluate` and `mind_api/src/endpoints/curriculum.py` `evaluate()` —
can refresh the metric in-process before evaluating gates. Mirrors the
`_team_state.py` routing/compose SSOT pattern.

Why refresh-before-evaluate lives HERE and not in precheck pseudocode:
the stale-metric class (g-115-1801 delta-froze-at-Foundation; g-115-2026
zeta assessed 2026-05-15 then never again) survived because the producer
was LLM-discretionary. Wiring the refresh inside the evaluate chokepoint
makes it script-enforced on every path (Phase 0.5i cadence,
/curriculum-gates, evolve, manual).

PURE MODULE CONTRACT: no `_paths` import, no module-level path constants —
all functions take explicit `world_dir` / `agent_dir` Path args so the
daemon can pass per-request ctx paths (see .claude/rules/path-resolution.md
"Standard for daemon endpoints").
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

# Normalization constants. Adjust here if calibration drifts.
N_KNOWLEDGE = 50      # target tree-node count for "broad baseline"
N_PIPELINE = 5        # target (resolved + 0.5*active) for "judgment applied"
N_ENCODED = 20        # target (rb + 0.5*guard) for "lessons captured"
N_COMPLETION = 25     # target completed-goals for "work delivered"

# The one metric dotpath this producer owns (curriculum.yaml gate `metric`).
COMPETENCE_METRIC = "developmental-stage.current_assessment.average_competence"

PRODUCER = "core/scripts/competence-assess.py"

# Stage-assessment constants (4). Mirror of
# core/config/developmental-stage.yaml `exploration_budget.competence_mapping`
# and the per-stage tree_maturity bands. Formula was previously LLM-executed
# pseudocode in aspirations-evolve Step 0 — LLM-discretionary producers drift
# silent (rb-3171: zeta wrote once in 2 months; ZDS agents pinned at
# 'exploring' with forge gated). Script-enforcing it here gives the stage
# block ONE deterministic producer refreshed at the same chokepoints as the
# competence metric. Levels absent from the mapping (e.g. REFERENCE = lookup
# data, not competence) are skipped from the mean.
COMPETENCE_MAPPING = {"EXPLORE": 0.15, "CALIBRATE": 0.45, "EXPLOIT": 0.70, "MASTER": 0.90}

# (stage_label, exclusive upper bound on tree_maturity); None = no upper bound.
STAGE_BANDS = [
    ("exploring", 0.30),
    ("developing", 0.55),
    ("applying", 0.80),
    ("mastering", None),
]


def stage_for_maturity(tree_maturity: float) -> str:
    for label, upper in STAGE_BANDS:
        if upper is None or tree_maturity < upper:
            return label
    return STAGE_BANDS[-1][0]


def assess_stage(world_dir: Path) -> dict:
    """Compute the developmental-stage block from tree-leaf capability levels.

    tree_maturity = mean mapped capability_level of tree LEAVES at depth >= 2
    (a leaf = a node that is no other node's parent). Drives the stage label,
    exploration_budget = clamp(1 - tree_maturity, 0.15, 0.85), and
    highest/lowest capability. Empty tree (or no mappable leaves) yields the
    initial-state values (exploring / 0.0 / 0.85 / EXPLORE).
    """
    tree_path = world_dir / "knowledge" / "tree" / "_tree.yaml"
    nodes = {}
    if tree_path.exists():
        tree = yaml.safe_load(tree_path.read_text(encoding="utf-8")) or {}
        nodes = tree.get("nodes", {}) or {}
    parents = {v.get("parent") for v in nodes.values() if isinstance(v, dict) and v.get("parent")}
    mapped = []
    unmapped_skipped = 0
    for key, v in nodes.items():
        if not isinstance(v, dict) or key in parents:
            continue
        depth = v.get("depth")
        if not isinstance(depth, (int, float)) or depth < 2:
            continue
        val = COMPETENCE_MAPPING.get(str(v.get("capability_level")))
        if val is None:
            unmapped_skipped += 1
            continue
        mapped.append(val)
    if mapped:
        tree_maturity = round(sum(mapped) / len(mapped), 4)
        highest = max(mapped)
        lowest = min(mapped)
        by_value = {v: k for k, v in COMPETENCE_MAPPING.items()}
        highest_capability = by_value[highest]
        lowest_capability = by_value[lowest]
    else:
        tree_maturity = 0.0
        highest_capability = "EXPLORE"
        lowest_capability = "EXPLORE"
    return {
        "tree_maturity": tree_maturity,
        "stage": stage_for_maturity(tree_maturity),
        "highest_capability": highest_capability,
        "lowest_capability": lowest_capability,
        "exploration_budget": round(max(0.15, min(0.85, 1.0 - tree_maturity)), 4),
        "leaves_counted": len(mapped),
        "unmapped_skipped": unmapped_skipped,
    }


def count_tree_nodes_with_files(world_dir: Path) -> int:
    tree_path = world_dir / "knowledge" / "tree" / "_tree.yaml"
    if not tree_path.exists():
        return 0
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8")) or {}
    nodes = tree.get("nodes", {}) or {}
    return sum(1 for v in nodes.values() if isinstance(v, dict) and v.get("file"))


def count_jsonl(path: Path, predicate) -> int:
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if predicate(r):
                n += 1
        except json.JSONDecodeError:
            continue
    return n


def count_completed_goals(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            asp = json.loads(line)
            for g in asp.get("goals", []):
                if g.get("status") == "completed":
                    n += 1
        except json.JSONDecodeError:
            continue
    return n


def assess(world_dir: Path, agent_dir: Path) -> dict:
    tree_nodes = count_tree_nodes_with_files(world_dir)
    pipeline_path = world_dir / "pipeline.jsonl"
    resolved = count_jsonl(pipeline_path, lambda r: r.get("stage") == "resolved")
    active = count_jsonl(pipeline_path, lambda r: r.get("stage") == "active")
    rb_active = count_jsonl(
        world_dir / "reasoning-bank.jsonl",
        lambda r: r.get("status", "active") == "active",
    )
    guard_active = count_jsonl(
        world_dir / "guardrails.jsonl",
        lambda r: r.get("status", "active") == "active",
    )
    world_completed = count_completed_goals(world_dir / "aspirations.jsonl")
    agent_completed = count_completed_goals(agent_dir / "aspirations.jsonl")
    total_completed = world_completed + agent_completed

    knowledge_density = min(1.0, tree_nodes / N_KNOWLEDGE)
    pipeline_activity = min(1.0, (resolved + 0.5 * active) / N_PIPELINE)
    encoded_lessons = min(1.0, (rb_active + 0.5 * guard_active) / N_ENCODED)
    completion_breadth = min(1.0, total_completed / N_COMPLETION)

    average = round(
        (knowledge_density + pipeline_activity + encoded_lessons + completion_breadth) / 4,
        4,
    )

    return {
        "average_competence": average,
        "components": {
            "knowledge_density": round(knowledge_density, 4),
            "pipeline_activity": round(pipeline_activity, 4),
            "encoded_lessons": round(encoded_lessons, 4),
            "completion_breadth": round(completion_breadth, 4),
        },
        "evidence": {
            "tree_nodes_with_files": tree_nodes,
            "pipeline_resolved": resolved,
            "pipeline_active": active,
            "reasoning_bank_active": rb_active,
            "guardrails_active": guard_active,
            "completed_goals_world": world_completed,
            "completed_goals_agent": agent_completed,
            "completed_goals_total": total_completed,
        },
        "normalization": {
            "N_knowledge": N_KNOWLEDGE,
            "N_pipeline": N_PIPELINE,
            "N_encoded": N_ENCODED,
            "N_completion": N_COMPLETION,
        },
        "stage_assessment": assess_stage(world_dir),
    }


def write_developmental_stage(agent_dir: Path, result: dict) -> Path:
    """Merge the assessment into <agent>/developmental-stage.yaml (atomic).

    Always writes `current_assessment.{average_competence,assessed_at,
    components,evidence,producer}`. When the result carries a
    `stage_assessment` block (every full assess() since g-115-2624), ALSO
    writes the stage block — overall_stage, current_assessment.{stage,
    tree_maturity,highest_capability,lowest_capability,exploration_budget,
    resolved_hypotheses}, exploration.epsilon — so the producer stamp is
    honest: one producer writes every field it stamps. resolved_hypotheses
    dedupes to evidence.pipeline_resolved (single source; the old
    reflect-on-outcome LLM increment never fired and sat contradicting its
    own sibling evidence). Results WITHOUT stage_assessment (legacy/partial
    callers) preserve stage fields untouched. schema_operations and any
    other top-level keys are always preserved.
    """
    stage_path = agent_dir / "developmental-stage.yaml"
    if stage_path.exists():
        doc = yaml.safe_load(stage_path.read_text(encoding="utf-8")) or {}
    else:
        doc = {}
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ca = doc.setdefault("current_assessment", {})
    ca["average_competence"] = result["average_competence"]
    ca["assessed_at"] = ts
    ca["components"] = result["components"]
    ca["evidence"] = result["evidence"]
    ca["producer"] = PRODUCER
    sa = result.get("stage_assessment")
    if isinstance(sa, dict):
        doc["overall_stage"] = sa["stage"]
        doc["last_updated"] = ts
        ca["stage"] = sa["stage"]
        ca["tree_maturity"] = sa["tree_maturity"]
        ca["highest_capability"] = sa["highest_capability"]
        ca["lowest_capability"] = sa["lowest_capability"]
        ca["exploration_budget"] = sa["exploration_budget"]
        ca["resolved_hypotheses"] = result.get("evidence", {}).get("pipeline_resolved", 0)
        exploration = doc.setdefault("exploration", {})
        exploration["epsilon"] = sa["exploration_budget"]
    tmp = Path(str(stage_path) + ".tmp")
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    os.replace(str(tmp), str(stage_path))
    return stage_path


def refresh_competence_for_gates(gates, world_dir, agent_dir) -> str:
    """Refresh the competence metric iff a gate consumes it. FAIL-OPEN.

    Called by both curriculum evaluate implementations immediately before
    the gate loop. Returns a status string surfaced in the evaluate JSON:
      "ok"       — a metric_threshold gate targets COMPETENCE_METRIC and the
                   assessment was recomputed + written (assessed_at bumped)
      "skipped"  — no gate in this stage consumes the metric; nothing done
      "failed: <err>" — refresh raised; evaluate proceeds with the stored
                   (possibly stale) value, exactly the pre-wiring behavior.
    Never raises: a producer failure must not block gate evaluation.
    """
    try:
        consumes = any(
            g.get("type") == "metric_threshold" and g.get("metric") == COMPETENCE_METRIC
            for g in gates
            if isinstance(g, dict)
        )
        if not consumes:
            return "skipped"
        result = assess(Path(world_dir), Path(agent_dir))
        write_developmental_stage(Path(agent_dir), result)
        return "ok"
    except Exception as exc:  # fail-open by contract
        try:
            print(f"[_competence] refresh failed (stale value used): {exc}", file=sys.stderr)
        except Exception:
            pass
        return f"failed: {exc}"
