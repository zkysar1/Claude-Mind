"""test_routing_audit_target_status_sweep.py — regression test for 3 / rb-1478.

Asserts that routing-audit-target-status-sweep.py's helper functions correctly
identify the g-115-1329 canonical incident shape (a routing-audit Investigate
goal whose TARGET reached terminal status) AND reject false-positive cases that
would have leaked through a less-conservative heuristic.

Cases covered:
  1. target_id parser: origin_signal "routing-mismatch:g-001-17" -> g-001-17
  2. target_id parser: origin_signal "routing-either-resolve:g-115-1142" -> g-115-1142
  3. target_id parser: title "Investigate: routing-mismatch g-315-81 ..." -> g-315-81
       (fallback when origin_signal absent)
  4. target_id parser: origin_signal wins over title when both present
  5. target_id parser: nothing parseable -> None (g-115-1100 generic shape:
       "Investigate: _goal_source.infer() prefix table missing ...")
  6. class matcher: discovered_by constant yes; origin_signal yes; title yes;
       unrelated "Investigate: foo" no; "Unblock: foo" no (sibling-sweep domain)
  7. idempotency check: outcome_note already starts with the sweep phrase ->
       considered already swept
  8. terminal-state set: completed/archived/skipped/superseded are sweep
       targets; pending/in-progress are NOT
  9. discovered_by is NOT a target-id source (constant, not a goal-id) — a goal
       whose ONLY routing signal is discovered_by has target_id == None

Pattern: same importlib + sys.path shape as test_unblock_parent_status_sweep.py.
routing-audit-target-status-sweep.py uses a hyphenated filename so we load it via
spec_from_file_location with a hyphen-free attribute name.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_sweep():
    """Load routing-audit-target-status-sweep.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "routing_audit_target_status_sweep_mod",
        CORE_SCRIPTS / "routing-audit-target-status-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "could not load spec for routing-audit-target-status-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- Case 1-5: target_id extraction ---------------------------------------

def test_target_id_from_origin_signal_mismatch():
    """9 canonical: _build_investigate_spec emits
    origin_signal='routing-mismatch:<target-id>' verbatim."""
    mod = _import_sweep()
    g = {
        "id": "g-115-1094",
        "title": "Investigate: routing-mismatch g-001-17 intended_agent=zeta but bravo's Self.md domains match",
        "origin_signal": "routing-mismatch:g-001-17",
    }
    assert mod._parse_target_id(g) == "g-001-17"


def test_target_id_from_origin_signal_either_resolve():
    """_build_either_resolve_spec emits origin_signal='routing-either-resolve:<id>'."""
    mod = _import_sweep()
    g = {
        "id": "g-115-1143",
        "title": "Investigate: routing-either-resolve g-115-1142 intended_agent=either but bravo's Self.md domains strongly match",
        "origin_signal": "routing-either-resolve:g-115-1142",
    }
    assert mod._parse_target_id(g) == "g-115-1142"


def test_target_id_from_title_fallback():
    """When origin_signal absent, parse the target from the title shape
    'routing-(mismatch|either-resolve) <g-id> ...'."""
    mod = _import_sweep()
    g = {
        "id": "g-115-1095",
        "title": "Investigate: routing-mismatch g-315-81 intended_agent=zeta but bravo's Self.md domains match",
        "origin_signal": "",
    }
    assert mod._parse_target_id(g) == "g-315-81"


def test_target_id_origin_signal_wins_over_title():
    """origin_signal is the priority source; title is fallback only."""
    mod = _import_sweep()
    g = {
        "id": "g-x",
        "title": "Investigate: routing-mismatch g-999-99 intended_agent=...",
        "origin_signal": "routing-mismatch:g-001-17",
    }
    assert mod._parse_target_id(g) == "g-001-17"


def test_target_id_unparseable_generic_shape():
    """0 shape: a routing-audit goal with a generic title carrying no
    target goal-id and no target-shaped origin_signal -> None (correctly not
    a sweep candidate)."""
    mod = _import_sweep()
    g = {
        "id": "g-115-1100",
        "title": "Investigate: _goal_source.infer() prefix table missing alert-email/routing prefixes",
        "origin_signal": "investigate:goal-source-infer-prefix-table",
        "discovered_by": "post-decompose-routing-audit",
    }
    assert mod._parse_target_id(g) is None


# ---- Case 6 & 9: class membership -----------------------------------------

def test_is_routing_audit_goal_discovered_by_constant():
    """discovered_by == 'post-decompose-routing-audit' is the most reliable
    class signal."""
    mod = _import_sweep()
    g = {"id": "g-x", "title": "anything", "discovered_by": "post-decompose-routing-audit"}
    assert mod._is_routing_audit_goal(g) is True


def test_is_routing_audit_goal_origin_signal():
    mod = _import_sweep()
    g = {"id": "g-x", "title": "weird title", "origin_signal": "routing-either-resolve:g-001-256"}
    assert mod._is_routing_audit_goal(g) is True


def test_is_routing_audit_goal_title():
    mod = _import_sweep()
    g = {"id": "g-x", "title": "Investigate: routing-mismatch g-315-82 intended_agent=echo ..."}
    assert mod._is_routing_audit_goal(g) is True


def test_is_routing_audit_goal_rejects_unrelated_investigate():
    """A plain Investigate goal that is NOT a routing-audit goal must not match."""
    mod = _import_sweep()
    g = {"id": "g-x", "title": "Investigate: auto-categorizer assigns wrong category to non-telemetry goals"}
    assert mod._is_routing_audit_goal(g) is False


def test_is_routing_audit_goal_rejects_unblock():
    """Unblock goals belong to the sibling unblock-parent-status-sweep, not here."""
    mod = _import_sweep()
    g = {"id": "g-250-73", "title": "Unblock: behavior for g-250-69",
         "origin_signal": "unblock:g-250-69"}
    assert mod._is_routing_audit_goal(g) is False


def test_discovered_by_alone_yields_no_target():
    """discovered_by is the CONSTANT discoverer name, never a target id — a goal
    whose only routing signal is discovered_by is class-matched but yields no
    parseable target (so it is correctly NOT swept)."""
    mod = _import_sweep()
    g = {"id": "g-x", "title": "Investigate: routing-mismatch prefix-table fix",
         "discovered_by": "post-decompose-routing-audit", "origin_signal": ""}
    assert mod._is_routing_audit_goal(g) is True
    assert mod._parse_target_id(g) is None


# ---- Case 7: idempotency ---------------------------------------------------

def test_is_already_swept_true():
    mod = _import_sweep()
    g = {"outcome_note": "routing-audit target resolved without action needed "
                         "(target_id=g-115-1328, target.status=completed)"}
    assert mod._is_already_swept(g) is True


def test_is_already_swept_false():
    mod = _import_sweep()
    g = {"outcome_note": "some other note"}
    assert mod._is_already_swept(g) is False
    assert mod._is_already_swept({}) is False


# ---- Case 8: terminal-state set -------------------------------------------

def test_terminal_states_set():
    mod = _import_sweep()
    assert mod.TERMINAL_STATES == {"completed", "archived", "skipped", "superseded"}
    for s in ("completed", "archived", "skipped", "superseded"):
        assert s in mod.TERMINAL_STATES
    for s in ("pending", "in-progress"):
        assert s not in mod.TERMINAL_STATES


# ---- status index helper ---------------------------------------------------

def test_build_status_index():
    mod = _import_sweep()
    asps = [
        ({"id": "asp-115", "goals": [
            {"id": "g-1", "status": "completed"},
            {"id": "g-2", "status": "pending"},
        ]}, "world"),
        ({"id": "asp-001", "goals": [
            {"id": "g-3", "status": "skipped"},
        ]}, "agent"),
    ]
    idx = mod._build_status_index(asps)
    assert idx == {"g-1": "completed", "g-2": "pending", "g-3": "skipped"}


# ---- 9: corrected-while-pending close reason ----------------------
# Description text below mirrors post-decompose-routing-audit._build_*_spec
# VERBATIM so these double as a regression guard if the clause format drifts.

# Canonical  routing-mismatch description (stamped alpha, recommend echo).
_MISMATCH_DESC_G315_219 = (
    "Routing-audit triggered: goal g-315-219 was stamped "
    "intended_agent=alpha (Jaccard 0.0455) but the highest Self.md domain-token "
    "Jaccard overlap was echo (0.0650, gap +0.0195). Review whether the routing "
    "should be corrected manually (aspirations-update-goal.sh g-315-219 "
    "intended_agent echo) or whether capability_route's tables need extending "
    "(sub-fix 1 / sub-fix 2 pattern). Source: post-decompose-routing-audit.py."
)
# either-resolve description (stamped either, recommend bravo).
_EITHER_DESC = (
    "Routing-audit triggered: goal g-115-1142 was stamped intended_agent=either "
    "(classifier uncertain) but the highest Self.md domain-token Jaccard overlap "
    "was bravo (0.0700, stand-out gap +0.0210 over second-best 0.0490). Suggest "
    "re-stamp either -> bravo: aspirations-update-goal.sh g-115-1142 "
    "intended_agent bravo. Surfaced under the either-case threshold."
)


def test_parse_recommended_agent_mismatch_form():
    """ shape: recommended agent (echo) parsed from the description's
    'aspirations-update-goal.sh <id> intended_agent <best_agent>)' clause."""
    mod = _import_sweep()
    g = {"id": "g-115-1528", "description": _MISMATCH_DESC_G315_219}
    assert mod._parse_recommended_agent(g) == "echo"


def test_parse_recommended_agent_either_resolve_form():
    """either-resolve description (clause closes with '.'): recommend bravo."""
    mod = _import_sweep()
    g = {"id": "g-115-1143", "description": _EITHER_DESC}
    assert mod._parse_recommended_agent(g) == "bravo"


def test_parse_recommended_agent_ignores_stamped_equals_form():
    """The STAMPED agent is written 'intended_agent=alpha' (equals) — the
    whitespace-required pattern must capture the RECOMMENDED (echo), never the
    stamped (alpha)."""
    mod = _import_sweep()
    g = {"id": "g-115-1528", "description": _MISMATCH_DESC_G315_219}
    rec = mod._parse_recommended_agent(g)
    assert rec == "echo"
    assert rec != "alpha"


def test_parse_recommended_agent_none_when_no_clause():
    """A description without the update-goal clause -> None (no auto-close)."""
    mod = _import_sweep()
    g = {"id": "g-x", "description": "Some unrelated investigate with no re-stamp clause."}
    assert mod._parse_recommended_agent(g) is None
    assert mod._parse_recommended_agent({}) is None


def test_build_intended_agent_index():
    mod = _import_sweep()
    asps = [
        ({"id": "asp-115", "goals": [
            {"id": "g-1", "intended_agent": "echo"},
            {"id": "g-2", "intended_agent": "either"},
        ]}, "world"),
        ({"id": "asp-001", "goals": [
            {"id": "g-3", "intended_agent": "alpha"},
        ]}, "agent"),
    ]
    idx = mod._build_intended_agent_index(asps)
    assert idx == {"g-1": "echo", "g-2": "either", "g-3": "alpha"}


def test_recommended_matches_current_corrected():
    """ canonical: audit recommends echo, target's CURRENT intended_agent
    is now echo (corrected alpha->echo) -> matched True (resolved by definition)."""
    mod = _import_sweep()
    g = {"id": "g-115-1528", "description": _MISMATCH_DESC_G315_219,
         "origin_signal": "routing-mismatch:g-315-219"}
    idx = {"": "echo"}  # target re-stamped to the recommendation
    matched, recommended, current = mod._recommended_matches_current(g, "g-315-219", idx)
    assert matched is True
    assert recommended == "echo"
    assert current == "echo"


def test_recommended_matches_current_not_yet_corrected():
    """Target still stamped alpha (not yet corrected to the recommended echo)
    -> matched False (the audit's flagged mismatch still stands)."""
    mod = _import_sweep()
    g = {"id": "g-115-1528", "description": _MISMATCH_DESC_G315_219,
         "origin_signal": "routing-mismatch:g-315-219"}
    idx = {"": "alpha"}  # still the stamped agent
    matched, recommended, current = mod._recommended_matches_current(g, "g-315-219", idx)
    assert matched is False
    assert recommended == "echo"
    assert current == "alpha"


def test_recommended_matches_current_other_agent_no_match():
    """Conservative: target corrected to a THIRD agent (delta), NOT the audit's
    recommendation (echo) -> matched False. A correction the audit did not vouch
    for must not auto-close the mismatch goal."""
    mod = _import_sweep()
    g = {"id": "g-115-1528", "description": _MISMATCH_DESC_G315_219,
         "origin_signal": "routing-mismatch:g-315-219"}
    idx = {"g-315-219": "delta"}
    matched, recommended, current = mod._recommended_matches_current(g, "g-315-219", idx)
    assert matched is False
    assert recommended == "echo"
    assert current == "delta"


def test_recommended_matches_current_target_absent_no_match():
    """Target absent from the index (e.g. archived) -> current is None -> matched
    False here. (Absence is handled by the terminal path in main(), not this
    pending-correction check.)"""
    mod = _import_sweep()
    g = {"id": "g-115-1528", "description": _MISMATCH_DESC_G315_219,
         "origin_signal": "routing-mismatch:g-315-219"}
    matched, recommended, current = mod._recommended_matches_current(g, "g-315-219", {})
    assert matched is False
    assert recommended == "echo"
    assert current is None
