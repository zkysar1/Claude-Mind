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

Implementation lives in `core/scripts/_competence.py` (SSOT, g-115-2026)
so both curriculum evaluate paths (CLI `cmd_evaluate` + daemon
`/v1/curriculum/evaluate`) refresh the metric in-process before gate
evaluation. This file is the CLI entry point for manual/diagnostic runs.

Usage:
    py -3 core/scripts/competence-assess.py            # compute + write + print JSON
    py -3 core/scripts/competence-assess.py --dry-run  # compute + print, no write
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR, AGENT_NAME
from _competence import assess, write_developmental_stage


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
