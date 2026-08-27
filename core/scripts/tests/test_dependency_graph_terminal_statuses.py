"""Every TERMINAL goal status must be excluded from the blocked_by graph ().

`build_graph` skips goals whose status is in `TERMINAL_STATUSES` and keeps the
rest as live blockers. So a status that is terminal in the goal VOCABULARY but
missing from that tuple does not merely look untidy — the goal stays in the
adjacency map and blocks its dependents permanently.

That was the live state for `superseded` on 2026-08-27: `aspirations.py`
already listed it in TERMINAL_GOAL_STATUSES and the write path accepted it,
while `_dependency_graph` did not, so closing a duplicate as superseded would
have wedged its dependents — strictly worse than the skipped-close it exists to
replace, and the exact failure the g-364-77..80 incident was about.

The divergence test is the point: core/scripts carries EIGHT independent
terminal-status constants with no shared SSOT, so the only thing that keeps this
one honest is asserting it against the vocabulary rather than against a literal.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dependency_graph import TERMINAL_STATUSES, build_graph  # noqa: E402


def _vocabulary_terminal_statuses():
    """The goal vocabulary's terminal set, read from its own SSOT."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "aspirations_mod", SCRIPTS / "aspirations.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aspirations_mod"] = mod
    spec.loader.exec_module(mod)
    return set(mod.TERMINAL_GOAL_STATUSES)


def test_graph_terminal_set_covers_the_goal_vocabulary():
    """No status may be terminal for the writer and non-terminal for the graph."""
    missing = _vocabulary_terminal_statuses() - set(TERMINAL_STATUSES)
    assert not missing, (
        "these statuses close a goal but still block its dependents forever: "
        f"{sorted(missing)} — add them to _dependency_graph.TERMINAL_STATUSES"
    )


def test_superseded_blocker_does_not_block_its_dependent():
    """The  scenario, pinned concretely rather than by set algebra."""
    index = {
        "g-1-1": {"status": "superseded", "blocked_by": []},
        "g-1-2": {"status": "pending", "blocked_by": ["g-1-1"]},
    }
    edges, _dangling = build_graph(index)
    assert "g-1-1" not in edges, "a superseded goal must not be a graph SOURCE"


def test_decomposed_blocker_does_not_block_its_dependent():
    """`decomposed` is terminal in the vocabulary and had the same omission."""
    index = {
        "g-2-1": {"status": "decomposed", "blocked_by": []},
        "g-2-2": {"status": "pending", "blocked_by": ["g-2-1"]},
    }
    edges, _dangling = build_graph(index)
    assert "g-2-1" not in edges


def test_a_pending_blocker_is_still_retained():
    """Positive control — the exclusion must not have widened to everything."""
    index = {
        "g-3-1": {"status": "pending", "blocked_by": ["g-3-9"]},
    }
    edges, _dangling = build_graph(index)
    assert "g-3-1" in edges, "a non-terminal goal must remain a graph source"
