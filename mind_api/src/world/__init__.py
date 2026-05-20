"""world-service module (Phase 5 — internal world/meta split).

The world-service owns `ctx.paths.world`-rooted state. Endpoints in this
package: reasoning_bank, pipeline, pipeline_write, pattern_signatures,
tree, tree_read, team_state.

INVARIANT (sec15 Phase-5 gate, enforced by
core/scripts/meta-imports-world-gate.py):
    Nothing under mind_api/src/meta/ may import mind_api.src.world or
    any module in this package. meta -> world import count MUST be 0.

The world-service may import only the shared-infra substrate at
mind_api/src/ root (jsonl_cache, yaml_cache, file_locks, history,
changelog) + mind_api.src.endpoints._jsonl_common (shared JSONL helpers)
+ core/scripts/_*.py pure helpers via sys.path injection. See
mind_api/src/INTERFACE.md for the full three-layer boundary
(shared substrate / world-service / meta-service).
"""
