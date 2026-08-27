"""Compute and write developmental competence score from concrete evidence.

Producer for the `developmental-stage.current_assessment.average_competence`
metric consumed by curriculum gate-type `metric_threshold` (see
`core/scripts/curriculum.py`). Without a producer, the metric stays at
its initial value (0.0) and metric-threshold gates can never pass —
curriculum-gate-asymmetry, 2026-05-21 (pipeline:
`2026-05-20_curriculum-gate-asymmetry`, outcome CORRECTED).

Formula: the gate metric is completion_breadth ALONE, capped at 1.0
(changed 2026-08-26, g-115-5153 — it was an equal-weighted mean of the four
components below until then; a stored value from before that date is NOT
comparable to one after it, guard-1881).

  completion_breadth  = completed_goals_where(completed_by == agent) / N4

  competence = completion_breadth    in [0.0, 1.0]

Three further components are still COMPUTED and REPORTED under `components`
for diagnostics, and are EXCLUDED from the metric:

  knowledge_density   = nodes_with_files / N1     # tree growth
  pipeline_activity   = (resolved + 0.5*active) / N2   # judgment applied
  encoded_lessons     = (rb_active + 0.5*guardrails_active) / N3   # lessons captured

Why they are excluded: each is WORLD-scoped by construction — the tree,
pipeline, reasoning bank and guardrails have no per-agent partition — so
they are identical for every agent sharing a world and cannot discriminate
between them. Measured 2026-08-26: five agents, four different assessment
timestamps, all reading average_competence exactly 1.0 with every component
saturated 22x-564x past its target. A per-agent graduation gate keyed to a
world-level measurement certified nothing. `completed_by` is the only
per-agent attribution the stores carry (reasoning-bank and guardrails have
no attribution field at all), so completion is the only component that can
carry the gate. The emitted `component_scope` map names each component
world or agent so a consumer never has to re-derive this.

N4 (N_COMPLETION_AGENT) counts BOTH the live aspirations store and
aspirations-archive.jsonl — the archive holds the large majority of
completed goals, and reading only the live store made the score sawtooth
downward on every archival run. It is calibrated against the live
cumulative per-agent spread so the fleet straddles both live gate
thresholds (0.25 and 0.55) with no agent pinned at the cap.

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
