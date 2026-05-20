"""meta-service module (Phase 5 — internal world/meta split).

The meta-service owns `ctx.paths.meta`-rooted state. Today it is exactly
one endpoint: `spark_questions` (spark-questions.jsonl lives under META).

INVARIANT (sec15 Phase-5 gate, enforced by
core/scripts/meta-imports-world-gate.py):
    Nothing under mind_api/src/meta/ may import mind_api.src.world or any
    world-domain endpoint module (reasoning_bank, pipeline,
    pipeline_write, pattern_signatures, tree, tree_read, team_state).
    meta -> world import count MUST be 0.

meta-service may import only the shared-infra substrate at
mind_api/src/ root (jsonl_cache, etc.) + mind_api.src.endpoints._jsonl_common
(shared JSONL helpers). See mind_api/src/INTERFACE.md for the full
three-layer boundary (shared substrate / world-service / meta-service).
"""
