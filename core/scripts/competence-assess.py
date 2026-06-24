"""Compute and write developmental competence score from concrete evidence.

Producer for the `developmental-stage.current_assessment.average_competence`
metric consumed by curriculum gate-type `metric_threshold` (see
`core/scripts/curriculum.py`). Without a producer, the metric stays at
its initial value (0.0) and metric-threshold gates can never pass —
curriculum-gate-asymmetry, 2026-05-21 (pipeline:
`2026-05-20_curriculum-gate-asymmetry`, outcome CORRECTED).

Formula: equal-weighted mean of four components, each capped at 1.0.

  knowledge_density   = nodes_with_files / N1     # tree growth
  pipeline_activity   = (resolved + 0.5*active) / N2   # judgment applied
  encoded_lessons     = (rb_active + 0.5*guardrails_active) / N3   # lessons captured
  completion_breadth  = completed_goals / N4      # work delivered

  competence = (k + p + e + c) / 4    in [0.0, 1.0]

Normalization constants (N1..N4) are calibrated so that a foundationally-
competent agent (baseline knowledge captured, a few hypotheses resolved,
~20 completed goals) lands near 0.5 — clearing Stage 1 gate (0.30) but
NOT auto-passing Stage 2 gate (0.50) without further evidence.

Domain-agnostic: reads framework state (tree, pipeline, reasoning-bank,
guardrails, completed goals) only — no domain strings.

Usage:
    py -3 core/scripts/competence-assess.py            # compute + write + print JSON
    py -3 core/scripts/competence-assess.py --dry-run  # compute + print, no write
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR, AGENT_NAME

# Normalization constants. Adjust here if calibration drifts.
N_KNOWLEDGE = 50      # target tree-node count for "broad baseline"
N_PIPELINE = 5        # target (resolved + 0.5*active) for "judgment applied"
N_ENCODED = 20        # target (rb + 0.5*guard) for "lessons captured"
N_COMPLETION = 25     # target completed-goals for "work delivered"


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
    }


def write_developmental_stage(agent_dir: Path, result: dict) -> Path:
    stage_path = agent_dir / "developmental-stage.yaml"
    if stage_path.exists():
        doc = yaml.safe_load(stage_path.read_text(encoding="utf-8")) or {}
    else:
        doc = {}
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ca = doc.setdefault("current_assessment", {})
    ca["average_competence"] = result["average_competence"]
    ca["assessed_at"] = ts
    ca["components"] = result["components"]
    ca["evidence"] = result["evidence"]
    ca["producer"] = "core/scripts/competence-assess.py"
    stage_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return stage_path


def main():
    dry_run = "--dry-run" in sys.argv
    if WORLD_DIR is None or AGENT_DIR is None:
        print("error: WORLD_DIR or AGENT_DIR unresolved (MIND_AGENT unset?)", file=sys.stderr)
        sys.exit(2)
    result = assess(WORLD_DIR, AGENT_DIR)
    if not dry_run:
        path = write_developmental_stage(AGENT_DIR, result)
        result["written_to"] = str(path)
    result["agent"] = AGENT_NAME
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
