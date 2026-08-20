"""Regression tests for core/scripts/premise-invalidation-sweep.py.

Covers two things that must not silently regress:

  1. THE THESIS (bravo, g-326-468). The traversal is worth having only because
     it is BIDIRECTIONAL and TRANSITIVE — an overturned parent typically does
     NOT cite the finding that overturned it, and goals filed from goals cite
     their immediate parent. If either property is lost the tool degrades to
     something a one-line grep already does.

  2. THE COVERAGE REPORT (g-115-6946). The citation graph is built from prose
     goal-ids, and a citation whose target is absent from the loaded stores is
     unusable and gets dropped. Dropping an edge can only REMOVE candidates,
     never add one, so an unreported drop is a false negative that reads
     exactly like a clean all-clear. Measured on the live corpus at the time
     of writing: 51.7% of edges dropped. The counts must therefore be
     reported, and the severed-path case below is why they matter.

Hermetic: builds synthetic goal dicts and calls the functions directly. It
never reads the live aspiration stores and never writes anything.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path


def _find_repo_root() -> Path:
    """Walk upward for the dir holding the anchors this test needs, so it runs
    identically from core/scripts/tests or any other suite dir (guard-1037 —
    a hardcoded .parent chain is off-by-one from core/scripts/tests and lands
    on /repo/core)."""
    here = Path(__file__).resolve()
    needed = (
        ("core", "scripts", "premise-invalidation-sweep.py"),
        ("core", "scripts", "_paths.py"),
    )
    for anc in [here] + list(here.parents):
        if all((anc.joinpath(*parts)).exists() for parts in needed):
            return anc
    raise RuntimeError("repo root not found (need premise-invalidation-sweep.py + _paths.py)")


REPO_ROOT = _find_repo_root()
SCRIPTS = REPO_ROOT / "core" / "scripts"


def _load_module():
    """Import the hyphenated script by path (not importable as a module name)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "premise_invalidation_sweep", SCRIPTS / "premise-invalidation-sweep.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MOD = _load_module()


def _goal(gid, prose="", status="pending"):
    return {"id": gid, "title": "", "description": prose, "outcome_note": "", "status": status}


# --- coverage reporting () ------------------------------------------

def test_dropped_edges_are_counted_not_silently_absorbed():
    goals = {
        "g-100-01": _goal("g-100-01", "derived from g-100-02 and also g-999-99"),
        "g-100-02": _goal("g-100-02"),
    }
    _out, _in, stats = MOD.build_graph(goals)
    assert stats["edges_total"] == 2
    assert stats["edges_resolved"] == 1
    assert stats["edges_dropped"] == 1
    assert stats["dropped_pct"] == 50.0
    assert stats["unresolvable_ids"] == 1


def test_fully_resolvable_graph_reports_zero_dropped():
    """Positive control: the counter must be able to report zero, so a zero in
    the live report means 'nothing dropped' and not 'counter is broken'."""
    goals = {
        "g-100-01": _goal("g-100-01", "derived from g-100-02"),
        "g-100-02": _goal("g-100-02"),
    }
    _out, _in, stats = MOD.build_graph(goals)
    assert stats["edges_dropped"] == 0
    assert stats["dropped_pct"] == 0.0
    assert stats["unresolvable_ids"] == 0
    assert stats["edges_resolved"] == 1


def test_empty_graph_does_not_divide_by_zero():
    _out, _in, stats = MOD.build_graph({"g-100-01": _goal("g-100-01")})
    assert stats["edges_total"] == 0
    assert stats["dropped_pct"] == 0.0


def test_self_citation_is_not_an_edge():
    goals = {"g-100-01": _goal("g-100-01", "see g-100-01 for context")}
    _out, _in, stats = MOD.build_graph(goals)
    assert stats["edges_total"] == 0


# --- the thesis: bidirectional + transitive -----------------------------------

def test_walk_reaches_a_parent_that_does_not_cite_the_seed():
    """The load-bearing case. The overturned parent cites the seed NOWHERE;
    only the seed cites it. Outbound-only traversal can never reach it."""
    goals = {
        "g-200-01": _goal("g-200-01", "supersedes the diagnosis in g-200-02"),  # seed
        "g-200-02": _goal("g-200-02", "no citation of the finding at all"),     # parent
    }
    out, inb, _stats = MOD.build_graph(goals)
    found = MOD.walk("g-200-01", out, inb, 2)
    assert "g-200-02" in found


def test_walk_is_transitive_at_two_hops():
    """A goal filed from a goal filed from the seed cites only its immediate
    parent, so it is reachable at 2 hops and not at 1."""
    goals = {
        "g-200-01": _goal("g-200-01"),
        "g-200-02": _goal("g-200-02", "filed from g-200-01"),
        "g-200-03": _goal("g-200-03", "filed from g-200-02"),
    }
    out, inb, _stats = MOD.build_graph(goals)
    assert "g-200-03" not in MOD.walk("g-200-01", out, inb, 1)
    assert "g-200-03" in MOD.walk("g-200-01", out, inb, 2)


def test_a_dropped_intermediate_severs_the_transitive_path():
    """Why the coverage counters exist. The only route from the seed to
    g-300-03 runs through g-300-02, which is absent from the loaded stores.
    The path is severed, g-300-03 is invisible, and the ONLY signal that
    anything was missed is the dropped-edge count."""
    goals = {
        "g-300-01": _goal("g-300-01"),                                  # seed
        "g-300-03": _goal("g-300-03", "filed from g-300-02"),           #  absent
    }
    out, inb, stats = MOD.build_graph(goals)
    found = MOD.walk("g-300-01", out, inb, 3)
    assert "g-300-03" not in found, "expected the severed path to hide this goal"
    assert stats["edges_dropped"] == 1, "the severed path must be visible in the counters"


# --- own-cloud staleness warning (rb-2636) ------------------------------------

def test_backend_warning_fires_on_own_cloud(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "own-cloud")
    warning = MOD.stale_backend_warning()
    assert warning is not None
    assert "rb-2636" in warning


def test_backend_warning_silent_on_local_and_unset(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    assert MOD.stale_backend_warning() is None
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    assert MOD.stale_backend_warning() is None


def test_backend_warning_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "  Own-Cloud  ")
    assert MOD.stale_backend_warning() is not None


# --- report-only invariant (guard-1227 / guard-1231) --------------------------

def test_module_has_no_mutation_surface():
    """The tool's restraint is a documented design property, not an accident.
    If a future edit gives it a write path, this fails and the author has to
    justify it against the report-only rationale in the docstring.

    Checked structurally over the AST, NOT by substring. The first draft of
    this test grepped the raw source for 'subprocess' and failed on the word
    appearing in a PROSE comment about guard-744 — a matcher aimed at a raw
    blob instead of the parsed structure, which is the same defect class as
    guard-1571. Prose must be free to discuss a hazard the code does not have.
    """
    tree = ast.parse((SCRIPTS / "premise-invalidation-sweep.py").read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported, "report-only tool must not shell out"
    assert "shutil" not in imported, "report-only tool must not move/copy files"

    writers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name in {"write_text", "write_bytes", "unlink", "rename", "mkdir", "rmtree"}:
            writers.append(name)
        elif name == "open":
            # Path.open()/open() default to read; only an explicit w/a/x mode
            # is a write surface.
            modes = [a.value for a in node.args[1:]
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            modes += [kw.value.value for kw in node.keywords
                      if kw.arg == "mode" and isinstance(kw.value, ast.Constant)]
            if any(any(c in m for c in "wax") for m in modes):
                writers.append(f"open(mode={modes})")
    assert not writers, f"unexpected mutation surface: {writers}"
