"""test_audit_open_coverage.py — the shared per-flagged-id audit-coverage rule
(g-115-11720), used by BOTH precheck filing lanes (defer-drift 0.5b.10 and
reason-less-blocked 0.5b.11).

The module is pure (no I/O, no daemon), so these tests exercise it directly:

  * open_audits filters on origin_signal AND open status (pending /
    in-progress) — a completed audit does not hold the dedup;
  * naming_surface is title + description, lowercased;
  * covered_by is WORD-BOUNDARIED: an audit naming g-115-11720 does NOT
    cover g-115-117 (prefix siblings share per-aspiration prefixes), and an
    exact standalone token DOES;
  * an unreadable (empty title AND description) surface is treated as
    covering — fail-closed: a cross-box duplicate never self-heals;
  * uncovered_ids returns exactly the flagged ids no open audit names
    (the filing predicate for both lanes).

Pattern: importlib load (the module name is plain, but the rest of this
suite's siblings use the same loader shape for consistency).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "audit_open_coverage.py"

KEY = "investigate:defer-drift-audit"


def _import():
    spec = importlib.util.spec_from_file_location("audit_open_coverage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys_modules = __import__("sys").modules
    if "audit_open_coverage" in sys_modules:
        return sys_modules["audit_open_coverage"]
    sys_modules["audit_open_coverage"] = mod
    spec.loader.exec_module(mod)
    return mod


def _audit(goal_id, status="pending", key=KEY, title=None, description=None):
    g = {"id": goal_id, "status": status, "origin_signal": key}
    if title is not None:
        g["title"] = title
    if description is not None:
        g["description"] = description
    return g


# ── open_audits: the open filter ────────────────────────────────────────────

def test_open_audits_filters_on_key_and_status():
    mod = _import()
    goals = [
        _audit("g-1", "pending"),
        _audit("g-2", "in-progress"),
        _audit("g-3", "completed"),      # terminal -> not open
        _audit("g-4", "skipped"),        # terminal -> not open
        {"id": "g-5", "status": "pending",
         "origin_signal": "investigate:reason-less-blocked-audit"},  # other lane
        {"id": "g-6", "status": "pending"},  # no origin_signal
    ]
    ids = [a["id"] for a in mod.open_audits(goals, KEY)]
    assert ids == ["g-1", "g-2"]


def test_open_audits_tolerates_empty_and_non_dict():
    mod = _import()
    assert mod.open_audits(None, KEY) == []
    assert mod.open_audits([None, "x", 3], KEY) == []


# ── naming_surface ──────────────────────────────────────────────────────────

def test_naming_surface_is_lowercased_title_plus_description():
    mod = _import()
    a = _audit("g-1", title="Re-gate G-115-9 Drift", description="member g-115-8")
    surface = mod.naming_surface(a)
    assert "re-gate g-115-9 drift" in surface
    assert "member g-115-8" in surface
    assert surface == surface.lower()


def test_naming_surface_missing_fields_do_not_raise():
    """Missing fields yield an all-whitespace surface (the join separator)
    — covered_by strip-checks it and treats it as unreadable. The separator
    is deliberate: a bare concatenation could glue a partial token from the
    title onto one from the description into a false id."""
    mod = _import()
    assert mod.naming_surface({}).strip() == ""
    assert mod.naming_surface(None).strip() == ""


# ── covered_by: word-boundary matching ──────────────────────────────────────

def test_covered_by_exact_standalone_token():
    mod = _import()
    a = _audit("g-1", title="re-gate 2 drifted defer(s) g-115-8 g-115-9")
    assert mod.covered_by(a, "g-115-8") is True
    assert mod.covered_by(a, "g-115-9") is True
    assert mod.covered_by(a, "g-115-10") is False


def test_covered_by_prefix_sibling_does_not_cover():
    """The incident shape: ids share per-aspiration prefixes. An audit
    naming g-115-11720 must NOT cover g-115-117 (bare substring would)."""
    mod = _import()
    a = _audit("g-1", title="re-gate g-115-11720")
    assert mod.covered_by(a, "g-115-117") is False
    assert mod.covered_by(a, "g-115-11720") is True


def test_covered_by_trailing_digits_does_not_cover():
    """ named in an audit must not cover  (suffix sibling)."""
    mod = _import()
    a = _audit("g-1", description="member g-115-115 listed here")
    assert mod.covered_by(a, "g-115-11") is False


def test_covered_by_in_description_counts():
    mod = _import()
    a = _audit("g-1", title="audit", description="drifted goals:\n  - g-115-42 [...]")
    assert mod.covered_by(a, "g-115-42") is True


def test_covered_by_empty_surface_is_covering_fail_closed():
    """An unreadable surface (title AND description empty/missing) cannot be
    checked -> treated as covering (suppress the filing). A cross-box
    duplicate never self-heals, so skip-on-uncertainty stays correct."""
    mod = _import()
    assert mod.covered_by({"id": "g-1", "status": "pending"}, "g-115-1") is True
    assert mod.covered_by(_audit("g-1", title="", description=""), "g-115-1") is True
    assert mod.covered_by(None, "g-115-1") is True


def test_covered_by_empty_goal_id_never_covered():
    mod = _import()
    a = _audit("g-1", title="names g-115-1")
    assert mod.covered_by(a, "") is False
    assert mod.covered_by(a, None) is False


# ── uncovered_ids: the filing predicate ─────────────────────────────────────

def test_uncovered_ids_all_covered():
    mod = _import()
    goals = [
        _audit("g-audit", title="re-gate g-115-1 g-115-2"),
        {"id": "g-115-1", "status": "pending"},
        {"id": "g-115-2", "status": "pending"},
    ]
    assert mod.uncovered_ids(goals, KEY, ["g-115-1", "g-115-2"]) == []


def test_uncovered_ids_partial_coverage_files_only_the_rest():
    """The outcome-2 scenario, shared shape: open audit names goal A; the
    new flagged member is goal B. B is uncovered -> gets filed; A is not."""
    mod = _import()
    goals = [
        _audit("g-audit", title="re-gate 1 drifted defer(s) g-115-1",
               description="drifted goals:\n  - g-115-1"),
        {"id": "g-115-1", "status": "pending"},
        {"id": "g-115-2", "status": "pending"},
    ]
    assert mod.uncovered_ids(goals, KEY, ["g-115-1", "g-115-2"]) == ["g-115-2"]


def test_uncovered_ids_stale_audit_names_none():
    """An open audit naming none of the current flagged ids is stale — it
    does NOT suppress a fresh filing (the class-keyed latch, g-115-5132)."""
    mod = _import()
    goals = [
        _audit("g-audit", title="re-gate 1 drifted defer(s) g-999-999",
               description="drifted goals:\n  - g-999-999"),
        {"id": "g-115-1", "status": "pending"},
    ]
    assert mod.uncovered_ids(goals, KEY, ["g-115-1"]) == ["g-115-1"]


def test_uncovered_ids_terminal_audit_does_not_cover():
    """A completed audit's naming surface is not live coverage — the
    violation may have recurred with new goals."""
    mod = _import()
    goals = [
        _audit("g-audit", status="completed",
               title="re-gate 1 drifted defer(s) g-115-1"),
        {"id": "g-115-1", "status": "pending"},
    ]
    assert mod.uncovered_ids(goals, KEY, ["g-115-1"]) == ["g-115-1"]


def test_uncovered_ids_empty_surface_audit_is_covering():
    mod = _import()
    goals = [
        _audit("g-audit"),  # no title, no description
        {"id": "g-115-1", "status": "pending"},
    ]
    assert mod.uncovered_ids(goals, KEY, ["g-115-1"]) == []


def test_uncovered_ids_ignores_falsy_flagged_ids():
    mod = _import()
    assert mod.uncovered_ids([], KEY, [None, "", "g-115-1"]) == ["g-115-1"]


def test_uncovered_ids_other_lane_key_does_not_cover():
    """Coverage is per lane: an open audit under the OTHER lane's key
    never covers this lane's flagged ids."""
    mod = _import()
    goals = [
        _audit("g-audit", key="investigate:reason-less-blocked-audit",
               title="reconcile g-115-1"),
        {"id": "g-115-1", "status": "pending"},
    ]
    assert mod.uncovered_ids(goals, KEY, ["g-115-1"]) == ["g-115-1"]
