"""Endpoint registry.

Each endpoint module defines one or more handler functions and registers them
into ROUTES. The server imports this package once and dispatches by exact
URL-path match. Path prefixes (e.g. `/v1/aspirations/`) are owned by their
endpoint module.

Adding a new endpoint:
  1. Create mind_api/src/endpoints/<name>.py
  2. Define `register(routes: dict) -> None`
  3. Import + call from `_load_all()` below
"""
from __future__ import annotations

from typing import Callable, Dict, Tuple

# (method, exact_path) -> handler(ctx) -> Response
Route = Tuple[str, str]
Routes = Dict[Route, Callable]


def load_all() -> Routes:
    """Build the route table by importing every endpoint module."""
    routes: Routes = {}
    from . import health as _health
    from . import aspirations as _aspirations
    from . import wm as _wm
    from . import wm_write as _wm_write
    from . import aspirations_write as _aspirations_write
    from . import admin as _admin
    # Phase 5 — world-service module (ctx.paths.world endpoints)
    from ..world import tree as _tree
    from ..world import pipeline as _pipeline
    from ..world import reasoning_bank as _reasoning_bank
    from ..world import pattern_signatures as _pattern_signatures
    from ..world import pattern_signatures_write as _pattern_signatures_write
    from ..world import team_state as _team_state
    from ..world import team_state_write as _team_state_write
    from ..world import tree_read as _tree_read
    from ..world import tree_write as _tree_write
    from ..world import pipeline_write as _pipeline_write
    # Batch 6 — skill-relations graph (WORLD-scoped; raw default-Dumper YAML writes)
    from ..world import skill_relations as _skill_relations
    # Batch 6 — skill-analytics (read-only reports; mixed meta/world/agent scope)
    from . import skill_analytics as _skill_analytics
    # Batch 6 — utilization-stats (read-only reports; world + mixed-scope rules audit)
    from . import utilization as _utilization
    # Batch 6 — skill-discovery (read-only behavioral analysis; mixed scope)
    from . import skill_discovery as _skill_discovery
    # Phase 5 — meta-service module (spark_questions lives under META)
    from ..meta import spark_questions as _spark_questions
    from ..meta import spark_questions_write as _spark_questions_write
    # Batch 6 — meta-strategy engines (META-scoped; generic locked writes, no summary)
    from ..meta import meta_dead_ends as _meta_dead_ends
    from ..meta import meta_impk as _meta_impk
    from ..meta import skill_evaluate as _skill_evaluate
    from ..meta import skill_quality_score as _skill_quality_score
    from ..meta import meta_experiment as _meta_experiment
    from ..meta import meta_transfer as _meta_transfer
    from ..meta import meta_generations as _meta_generations
    from ..meta import meta_backpressure as _meta_backpressure
    from ..meta import meta_yaml as _meta_yaml
    from ..meta import strategy_apply as _strategy_apply
    from . import experience as _experience
    from . import experience_write as _experience_write
    from . import journal as _journal
    from . import board as _board
    from . import board_write as _board_write
    # PR 5 — retrieve orchestrator (read-only path)
    from . import retrieve as _retrieve
    # PR 6 — aspirations cross-queue goal query
    from . import aspirations_query as _aspirations_query
    # H2 Wave 1 — generic store writer (store_registry-parameterized)
    from . import store as _store
    # Batch 5 — changelog read/stats (world-scoped; append already daemonized)
    from . import changelog as _changelog
    # Batch 5 — curriculum gate engine (agent-scoped; raw writes, no lock/history)
    from . import curriculum as _curriculum
    # Batch 5 — history list/diff/restore/prune/prune-legacy (4-base resolver)
    from . import history as _history_ep
    _health.register(routes)
    _aspirations.register(routes)
    _tree.register(routes)
    _wm.register(routes)
    _wm_write.register(routes)
    _aspirations_write.register(routes)
    _admin.register(routes)
    _pipeline.register(routes)
    _reasoning_bank.register(routes)
    _pattern_signatures.register(routes)
    _pattern_signatures_write.register(routes)
    _spark_questions.register(routes)
    _spark_questions_write.register(routes)
    _experience.register(routes)
    _experience_write.register(routes)
    _journal.register(routes)
    _board.register(routes)
    _board_write.register(routes)
    _team_state.register(routes)
    _team_state_write.register(routes)
    _tree_read.register(routes)
    _tree_write.register(routes)
    _retrieve.register(routes)
    _aspirations_query.register(routes)
    _pipeline_write.register(routes)
    _store.register(routes)
    _changelog.register(routes)
    _curriculum.register(routes)
    _history_ep.register(routes)
    _meta_dead_ends.register(routes)
    _meta_impk.register(routes)
    _skill_relations.register(routes)
    _skill_analytics.register(routes)
    _utilization.register(routes)
    _skill_discovery.register(routes)
    _skill_evaluate.register(routes)
    _skill_quality_score.register(routes)
    _meta_experiment.register(routes)
    _meta_transfer.register(routes)
    _meta_generations.register(routes)
    _meta_backpressure.register(routes)
    _meta_yaml.register(routes)
    _strategy_apply.register(routes)
    return routes
