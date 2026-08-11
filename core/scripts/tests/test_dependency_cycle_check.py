"""Tests for _dependency_graph.py + dependency-cycle-check.py ().

A dependency cycle is a property of the GRAPH, and every pre-existing check
inspects one EDGE, so a ring passes all of them simultaneously. These tests pin
the three things that make the sweep trustworthy:

  1. the polymorphic `blocked_by` normalization (a bare STRING must not explode
     into one phantom id per character — the defect the shared normalizer was
     written for);
  2. the graph's source/target asymmetry (terminal SOURCES excluded so finished
     work cannot manufacture a phantom ring; terminal TARGETS retained because
     they are what BREAKS a chain);
  3. cycle detection itself, including the two shapes most likely to be waved
     through — the degenerate SELF-LOOP, and one ring reachable from several
     entry points, which must report as ONE deadlock rather than several.

`_dependency_graph` is a plain module name and imports directly; the sweep has
hyphens in its filename, so it needs the importlib shape used by
test_blocked_signal_resolution_check.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dependency_graph import (  # noqa: E402
    build_graph,
    find_cycles,
    norm_blocked_by,
)

SWEEP = SCRIPTS / "dependency-cycle-check.py"


def _import_sweep():
    spec = importlib.util.spec_from_file_location("dependency_cycle_check", SWEEP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dependency_cycle_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _idx(**kw):
    """{id: goal} from id=(status, blocked_by) pairs."""
    return {k: {"id": k, "status": v[0], "blocked_by": v[1]}
            for k, v in kw.items()}


# --- 1. normalization ------------------------------------------------------

def test_bare_string_does_not_explode_into_characters():
    # The founding defect: iterating the raw field turns a 9-char id into 9
    # phantom single-character ids, none of which resolve.
    assert norm_blocked_by("g-335-260") == ["g-335-260"]


def test_norm_handles_list_none_and_junk():
    assert norm_blocked_by(None) == []
    assert norm_blocked_by([]) == []
    assert norm_blocked_by("") == []
    assert norm_blocked_by("   ") == []
    assert norm_blocked_by(["a", "b"]) == ["a", "b"]
    # Non-str members are DROPPED, never coerced — an unexpected shape must not
    # become a confident wrong id.
    assert norm_blocked_by(["a", None, 3, "  ", {"x": 1}]) == ["a"]
    assert norm_blocked_by({"not": "a list"}) == []


def test_shared_normalizer_is_the_same_object_the_sibling_uses():
    """guard-547 SSOT: the sweep and blocked-signal-resolution-check must share
    ONE normalizer, or the copies drift (the measured harm guard-547 names)."""
    spec = importlib.util.spec_from_file_location(
        "bsrc_for_ssot", SCRIPTS / "blocked-signal-resolution-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bsrc_for_ssot"] = mod
    spec.loader.exec_module(mod)
    assert mod._norm_blocked_by is norm_blocked_by


# --- 2. graph construction -------------------------------------------------

def test_terminal_sources_excluded_so_finished_work_makes_no_phantom_ring():
    edges, _ = build_graph(_idx(a=("completed", ["b"]), b=("completed", ["a"])))
    assert edges == {}
    assert find_cycles(edges) == []


def test_terminal_target_retained_and_breaks_the_chain():
    # b is completed: it stays in the index (so the edge is NOT dangling) but
    # contributes no outgoing edge, so there is no cycle.
    edges, dangling = build_graph(_idx(a=("pending", ["b"]), b=("completed", ["a"])))
    assert edges == {"a": ["b"]}
    assert dangling == []
    assert find_cycles(edges) == []


def test_dangling_edge_reported():
    _, dangling = build_graph(_idx(a=("pending", ["ghost"])))
    assert dangling == [("a", "ghost")]


def test_every_terminal_status_filters_the_source():
    for st in ("completed", "archived", "skipped", "expired", "resolved"):
        edges, _ = build_graph(_idx(a=(st, ["b"]), b=("pending", [])))
        assert edges == {}, f"status {st} should exclude the source"


def test_skipped_goal_with_live_edge_is_still_excluded_as_a_source():
    """guard-1690 names skipped-with-a-live-edge as a DEAD ZONE, but that is an
    argument for a different sweep to resurface it — not for this one to invent
    a ring out of work nobody is waiting on. Pinned so the intent is explicit
    rather than incidental."""
    edges, _ = build_graph(_idx(a=("skipped", ["b"]), b=("pending", ["a"])))
    assert "a" not in edges
    assert find_cycles(edges) == []


# --- 3. cycle detection ----------------------------------------------------

def test_self_loop_detected():
    # Cheapest form of the bug to write by hand; must never be filtered as
    # trivial.
    edges, _ = build_graph(_idx(a=("pending", "a")))
    assert find_cycles(edges) == [["a"]]


def test_two_goal_ring_the_founding_incident_shape():
    edges, _ = build_graph(_idx(x=("blocked", ["y"]), y=("blocked", ["x"])))
    assert find_cycles(edges) == [["x", "y"]]


def test_three_goal_ring():
    edges, _ = build_graph(_idx(a=("pending", ["b"]), b=("pending", ["c"]),
                                c=("pending", ["a"])))
    cycles = find_cycles(edges)
    assert len(cycles) == 1
    assert sorted(cycles[0]) == ["a", "b", "c"]


def test_dag_has_no_cycle():
    edges, _ = build_graph(_idx(a=("pending", ["b"]), b=("pending", ["c"]),
                                c=("pending", [])))
    assert find_cycles(edges) == []


def test_one_ring_reached_from_several_entries_reports_once():
    # z and w both lead into the a<->b ring. That is ONE deadlock; reporting it
    # three times would misstate severity to whoever reads the verdict.
    edges, _ = build_graph(_idx(z=("pending", ["a"]), w=("pending", ["a"]),
                                a=("pending", ["b"]), b=("pending", ["a"])))
    assert find_cycles(edges) == [["a", "b"]]


def test_two_independent_rings_both_reported():
    edges, _ = build_graph(_idx(a=("pending", ["b"]), b=("pending", ["a"]),
                                c=("pending", ["d"]), d=("pending", ["c"])))
    assert len(find_cycles(edges)) == 2


def test_deep_chain_does_not_raise_recursion_error():
    # Iterative walk on purpose: depth tracks chain length, and the sweep must
    # not die exactly when the graph is most degenerate.
    n = 3000
    idx = {str(i): {"id": str(i), "status": "pending", "blocked_by": [str(i + 1)]}
           for i in range(n)}
    idx[str(n)] = {"id": str(n), "status": "pending", "blocked_by": ["0"]}
    edges, _ = build_graph(idx)
    cycles = find_cycles(edges)
    assert len(cycles) == 1 and len(cycles[0]) == n + 1


def test_empty_graph_is_clean_not_crashing():
    edges, dangling = build_graph({})
    assert (edges, dangling) == ({}, [])
    assert find_cycles(edges) == []


# --- 4. the sweep's anti-vacuous-zero contract ------------------------------

def test_payload_reports_population_alongside_a_zero_verdict():
    """A bare `cycles_found: 0` is indistinguishable from a sweep that scanned
    nothing — the exact ambiguity that kept the founding incident invisible
    (rb-245 / guard-1922). These keys are what make a zero falsifiable, so they
    are pinned rather than left to convention."""
    mod = _import_sweep()
    src = SWEEP.read_text(encoding="utf-8")
    for key in ("goals_scanned", "goals_with_edges", "edges_total",
                "archive_degraded", "dangling_count", "verdict"):
        assert f'"{key}"' in src, f"payload must report {key} beside the verdict"
    # An empty population must NOT read as a clean bill of health.
    assert '"skipped-empty-population"' in src
    assert mod.SOURCES == ("world", "agent")


def test_sweep_has_no_apply_path():
    """Detective only. Breaking a cycle means choosing which edge is wrong, and
    that is a judgment about intent, not shape."""
    src = SWEEP.read_text(encoding="utf-8")
    # Test the MECHANISM (a registered argparse flag), not the substring: the
    # module docstring legitimately mentions `--apply` while explaining why
    # there is none, and a bare substring check fails on its own rationale.
    assert 'add_argument("--apply"' not in src
    assert "add_argument('--apply'" not in src
