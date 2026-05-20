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
    from . import aspirations_write as _aspirations_write
    from . import admin as _admin
    # Phase 5 — world-service module (ctx.paths.world endpoints)
    from ..world import tree as _tree
    from ..world import pipeline as _pipeline
    from ..world import reasoning_bank as _reasoning_bank
    from ..world import pattern_signatures as _pattern_signatures
    from ..world import team_state as _team_state
    from ..world import tree_read as _tree_read
    from ..world import pipeline_write as _pipeline_write
    # Phase 5 — meta-service module (spark_questions lives under META)
    from ..meta import spark_questions as _spark_questions
    from . import experience as _experience
    from . import journal as _journal
    from . import board as _board
    # PR 5 — retrieve orchestrator (read-only path)
    from . import retrieve as _retrieve
    # PR 6 — aspirations cross-queue goal query
    from . import aspirations_query as _aspirations_query
    # H2 Wave 1 — generic store writer (store_registry-parameterized)
    from . import store as _store
    _health.register(routes)
    _aspirations.register(routes)
    _tree.register(routes)
    _wm.register(routes)
    _aspirations_write.register(routes)
    _admin.register(routes)
    _pipeline.register(routes)
    _reasoning_bank.register(routes)
    _pattern_signatures.register(routes)
    _spark_questions.register(routes)
    _experience.register(routes)
    _journal.register(routes)
    _board.register(routes)
    _team_state.register(routes)
    _tree_read.register(routes)
    _retrieve.register(routes)
    _aspirations_query.register(routes)
    _pipeline_write.register(routes)
    _store.register(routes)
    return routes
