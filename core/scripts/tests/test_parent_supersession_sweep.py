"""test_parent_supersession_sweep.py — regression test for .

Asserts that parent-supersession-sweep.py's helper functions correctly
identify the g-268-10 canonical incident shape AND reject false-positive
cases that would have leaked through the v0 heuristic before the
aspiration-size guard was added.

Cases covered:
  1. Canonical incident: parent deferred + 2 siblings completed AFTER defer
     → both siblings identified as superseding
  2. Sibling completed BEFORE parent defer → excluded (parent was the
     consumer, not superseded)
  3. Sibling pending (not completed) → excluded
  4. Sibling title doesn't start with "Apply:" or "Design:" → excluded
  5. Parent has no defer_set_at and no created_at → empty result (cannot
     enforce temporal guard)
  6. Apply pattern matcher recognizes "Apply: foo", "  Apply : foo" (
     whitespace tolerant), rejects "Investigate: foo", "Idea: foo"
  7. Design/Apply pattern matcher recognizes "Design: foo" AND "Apply: foo"

The aspiration-size guard is tested separately via the smoke-test
documentation (see goal description) — running the script against the
live store. The unit tests here cover the per-goal logic.

Pattern: same importlib + sys.path shape as test_defer_recheck_patterns.py.
parent-supersession-sweep.py uses a hyphenated filename so we load it via
spec_from_file_location with a hyphen-free attribute name.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_sweep():
    """Load parent-supersession-sweep.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "parent_supersession_sweep_mod",
        CORE_SCRIPTS / "parent-supersession-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for parent-supersession-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso_hours_ago(hours: float) -> str:
    t = dt.datetime.now() - dt.timedelta(hours=hours)
    return t.isoformat(timespec="seconds")


def test_canonical_incident_shape():
    """ shape: parent deferred at T, 2 siblings completed at T+1h."""
    mod = _import_sweep()
    parent = {
        "id": "g-268-10",
        "title": "Apply: Implement BT-seed prefetch",
        "status": "pending",
        "defer_reason": "blocked_on_design",
        "defer_reason_set_at": _iso_hours_ago(48),
    }
    siblings = [
        parent,  # included to exercise the self-skip branch
        {
            "id": "g-268-15",
            "title": "Design: Option C for BT-seed",
            "status": "completed",
            "completed_at": _iso_hours_ago(47),  # AFTER parent defer (1h later)
        },
        {
            "id": "g-268-16",
            "title": "Apply: SeedGetterCache 119 LOC",
            "status": "completed",
            "completed_at": _iso_hours_ago(46),  # AFTER parent defer (2h later)
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert "g-268-15" in ids, f"expected g-268-15 in {ids}"
    assert "g-268-16" in ids, f"expected g-268-16 in {ids}"
    assert "g-268-10" not in ids, "self-skip failed"
    assert len(out) == 2


def test_sibling_completed_before_parent_defer_excluded():
    """Sibling completed BEFORE parent was deferred → parent consumed
    sibling work, not superseded by it. Exclude."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: do thing X",
        "status": "pending",
        "defer_reason": "wait",
        "defer_reason_set_at": _iso_hours_ago(10),
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: thing X core",
            "status": "completed",
            "completed_at": _iso_hours_ago(20),  # BEFORE parent defer
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    assert out == [], f"expected empty, got {out}"


def test_sibling_pending_excluded():
    """Pending siblings don't count — only completed work supersedes."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        "defer_reason_set_at": _iso_hours_ago(48),
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: X core",
            "status": "pending",  # not completed
        },
        {
            "id": "g-003",
            "title": "Apply: X done",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert ids == ["g-003"], f"expected only g-003, got {ids}"


def test_non_apply_design_title_excluded():
    """Investigate/Idea/Maintain title patterns are not supersession candidates."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        "defer_reason_set_at": _iso_hours_ago(48),
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Investigate: X cause",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
        {
            "id": "g-003",
            "title": "Idea: X variant",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
        {
            "id": "g-004",
            "title": "Maintain: X update",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    assert out == [], f"expected empty (no Apply/Design titles), got {out}"


def test_parent_without_reference_timestamp_returns_empty():
    """No defer_reason_set_at AND no created_at → cannot enforce temporal
    guard. Skip rather than treat all siblings as candidates."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        # No defer_reason_set_at or created_at
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: X done",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    assert out == [], f"expected empty (no ref ts), got {out}"


def test_apply_pattern_matcher():
    """Apply-pattern recognition: case + whitespace tolerant."""
    mod = _import_sweep()
    assert mod._is_apply_goal({"title": "Apply: foo"}) is True
    assert mod._is_apply_goal({"title": "  Apply : foo"}) is True
    assert mod._is_apply_goal({"title": "apply: foo"}) is True  # case-insens
    assert mod._is_apply_goal({"title": "Investigate: foo"}) is False
    assert mod._is_apply_goal({"title": "Design: foo"}) is False
    assert mod._is_apply_goal({"title": "Idea: foo"}) is False
    assert mod._is_apply_goal({"title": ""}) is False


def test_design_or_apply_pattern_matcher():
    """Design+Apply pattern recognition for sibling filtering."""
    mod = _import_sweep()
    assert mod._is_design_or_apply({"title": "Apply: foo"}) is True
    assert mod._is_design_or_apply({"title": "Design: foo"}) is True
    assert mod._is_design_or_apply({"title": "DESIGN: foo"}) is True
    assert mod._is_design_or_apply({"title": "Investigate: foo"}) is False
    assert mod._is_design_or_apply({"title": "Idea: foo"}) is False
    assert mod._is_design_or_apply({"title": ""}) is False


def test_fallback_to_created_at_when_no_defer_set():
    """If defer_reason_set_at is missing but created_at exists, use created_at
    as the temporal reference. Siblings completed after created_at qualify."""
    mod = _import_sweep()
    parent = {
        "id": "g-001",
        "title": "Apply: X",
        "status": "pending",
        "defer_reason": "wait",
        "created_at": _iso_hours_ago(48),  # no defer_reason_set_at
    }
    siblings = [
        {
            "id": "g-002",
            "title": "Apply: X done",
            "status": "completed",
            "completed_at": _iso_hours_ago(24),  # 24h after parent.created_at
        },
    ]
    out = mod._find_superseding_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert ids == ["g-002"], f"expected g-002 via created_at fallback, got {ids}"


# ── Structural split-parent lane (,  canonical shape) ──


def _g350_shape(sib_status_a="completed", sib_status_b="completed",
                blocked_by=("g-350-17", "g-350-18"), hours_since_completion=48.0):
    """Build the  canonical shape: non-Apply-titled parent split into
    two decomposition-backref siblings."""
    parent = {
        "id": "g-350-04",
        "title": "Feature 3 (Tools): the framework NPC Equips + Activates a Tool in DEV",
        "status": "pending",
        "blocked_by": list(blocked_by),
        "created_at": _iso_hours_ago(96),
    }
    siblings = [
        parent,
        {
            "id": "g-350-17",
            "title": "Feature 3a: Tool census + weld-on-touch equip demo in DEV",
            "status": sib_status_a,
            "origin_signal": "decomposition:g-350-04-equip-demo",
            "completed_at": _iso_hours_ago(hours_since_completion + 1),
        },
        {
            "id": "g-350-18",
            "title": "Feature 3b: NPC tool ACTIVATION mechanic — spec (M9)",
            "status": sib_status_b,
            "origin_signal": "decomposition:g-350-04-activation-mechanic",
            "completed_at": _iso_hours_ago(hours_since_completion),
        },
    ]
    return parent, siblings


def test_structural_lane_catches_g350_04_shape():
    """Non-Apply parent + 2 completed decomposition-backref siblings → both flagged."""
    mod = _import_sweep()
    parent, siblings = _g350_shape()
    out = mod._find_structural_split_siblings(parent, siblings)
    ids = sorted(s["id"] for s in out)
    assert ids == ["g-350-17", "g-350-18"], f"expected both siblings, got {ids}"


def test_structural_lane_discovered_by_backref():
    """discovered_by == parent_id qualifies as a backref too."""
    mod = _import_sweep()
    parent = {"id": "g-1", "title": "Feature X umbrella", "status": "pending"}
    siblings = [
        parent,
        {"id": "g-2", "title": "part 1", "status": "completed",
         "discovered_by": "g-1", "completed_at": _iso_hours_ago(48)},
        {"id": "g-3", "title": "part 2", "status": "completed",
         "discovered_by": "g-1", "completed_at": _iso_hours_ago(48)},
    ]
    ids = sorted(s["id"] for s in mod._find_structural_split_siblings(parent, siblings))
    assert ids == ["g-2", "g-3"], f"expected g-2+g-3, got {ids}"


def test_structural_lane_nonterminal_sibling_blocks_fire():
    """One decomposition sibling still pending → residual scope → NO fire
    (verification outcome 2: only fires when ALL split siblings complete)."""
    mod = _import_sweep()
    parent, siblings = _g350_shape(sib_status_b="pending")
    out = mod._find_structural_split_siblings(parent, siblings)
    assert out == [], f"expected empty (split in flight), got {out}"


def test_structural_lane_skipped_sibling_blocks_fire():
    """A SKIPPED decomposition child means its share of scope was not done —
    conservative: no fire."""
    mod = _import_sweep()
    parent, siblings = _g350_shape(sib_status_b="skipped")
    out = mod._find_structural_split_siblings(parent, siblings)
    assert out == [], f"expected empty (skipped child), got {out}"


def test_structural_lane_unresolved_blocked_by_blocks_fire():
    """parent.blocked_by naming a non-sibling (cross-aspiration dep) → the
    parent is a waiting consumer, not a superseded umbrella → no fire."""
    mod = _import_sweep()
    parent, siblings = _g350_shape(blocked_by=("g-350-17", "g-350-18", "g-999-01"))
    out = mod._find_structural_split_siblings(parent, siblings)
    assert out == [], f"expected empty (unknown dep in blocked_by), got {out}"


def test_structural_lane_no_backrefs_no_fire():
    """Completed siblings WITHOUT decomposition backrefs never qualify —
    the lane requires the explicit structural signal, not co-residence."""
    mod = _import_sweep()
    parent = {"id": "g-1", "title": "Feature X umbrella", "status": "pending"}
    siblings = [
        parent,
        {"id": "g-2", "title": "unrelated done", "status": "completed",
         "origin_signal": "idea:something-else", "completed_at": _iso_hours_ago(48)},
        {"id": "g-3", "title": "also unrelated", "status": "completed",
         "completed_at": _iso_hours_ago(48)},
    ]
    out = mod._find_structural_split_siblings(parent, siblings)
    assert out == [], f"expected empty (no backrefs), got {out}"


def test_structural_lane_prefix_collision_excluded():
    """A goal id that is a string-prefix of another id ( vs )
    must NOT absorb the longer id's decomposition children (boundary-anchored
    match — fresh-eyes finding 2026-07-18)."""
    mod = _import_sweep()
    parent = {"id": "g-350-17", "title": "Feature umbrella", "status": "pending"}
    siblings = [
        parent,
        {"id": "g-350-90", "title": "child of OTHER parent", "status": "completed",
         "origin_signal": "decomposition:g-350-171-part-a",
         "completed_at": _iso_hours_ago(48)},
        {"id": "g-350-91", "title": "true child", "status": "completed",
         "origin_signal": "decomposition:g-350-17-part-b",
         "completed_at": _iso_hours_ago(48)},
    ]
    out = mod._find_structural_split_siblings(parent, siblings)
    ids = [s["id"] for s in out]
    assert ids == ["g-350-91"], f"expected only the true child, got {ids}"


def test_structural_grace_window_age():
    """_newest_completion_age_hours returns hours since the NEWEST completion
    (the grace-window basis) and None when no timestamp parses."""
    mod = _import_sweep()
    sibs = [
        {"id": "a", "completed_at": _iso_hours_ago(50)},
        {"id": "b", "completed_at": _iso_hours_ago(10)},
    ]
    age = mod._newest_completion_age_hours(sibs)
    assert age is not None and 9.5 < age < 10.5, f"expected ~10h, got {age}"
    assert mod._newest_completion_age_hours([{"id": "c"}]) is None


if __name__ == "__main__":
    test_canonical_incident_shape()
    test_sibling_completed_before_parent_defer_excluded()
    test_sibling_pending_excluded()
    test_non_apply_design_title_excluded()
    test_parent_without_reference_timestamp_returns_empty()
    test_apply_pattern_matcher()
    test_design_or_apply_pattern_matcher()
    test_fallback_to_created_at_when_no_defer_set()
    test_structural_lane_catches_g350_04_shape()
    test_structural_lane_discovered_by_backref()
    test_structural_lane_nonterminal_sibling_blocks_fire()
    test_structural_lane_skipped_sibling_blocks_fire()
    test_structural_lane_unresolved_blocked_by_blocks_fire()
    test_structural_lane_no_backrefs_no_fire()
    test_structural_lane_prefix_collision_excluded()
    test_structural_grace_window_age()
    print("All 16 tests passed.")


# ── : recurring goals can never be supersession candidates ────────
# A recurring goal is a STANDING CADENCE; siblings that HARDEN what it invokes
# improve the cadence rather than retiring it (consolidate-before-expand.md
# rule 5). Measured before the fix:  (recurring, interval 16.2h,
# achievedCount=174, currentStreak=4) was reported eligible with
# action=mark_failed for 235h — ~10 days of write attempts refused only by a
# downstream guard the sweep never consults.
#
# These drive main() end-to-end by monkeypatching _read_aspirations, because
# the fix lives in main()'s loop, not in a helper — a helper-level test cannot
# reach it.

def _recurring_shape(recurring: bool):
    """g-350 structural shape whose parent is (or is not) a recurring goal."""
    parent, siblings = _g350_shape()
    parent = dict(parent)
    if recurring:
        parent.update({"recurring": True, "interval_hours": 16.2,
                       "achievedCount": 174, "currentStreak": 4,
                       "lastAchievedAt": _iso_hours_ago(72)})
    # siblings[0] IS the parent object in _g350_shape — rebuild with our copy.
    goals = [parent] + [s for s in siblings if s["id"] != parent["id"]]
    return [({"id": "asp-350", "status": "active", "goals": goals}, "world")]


def _run_main(monkeypatch, capsys, aspirations):
    import sys as _sys
    mod = _import_sweep()
    monkeypatch.setattr(mod, "_read_aspirations",
                        lambda source: aspirations if source == "world" else [])
    monkeypatch.setattr(mod, "_append_metric", lambda *a, **k: None)
    monkeypatch.setattr(_sys, "argv", [
        "parent-supersession-sweep.py", "--max-age-hours", "24",
        "--min-siblings", "2", "--metrics-log", "", "--output", "json",
    ])
    assert mod.main() == 0
    return json.loads(capsys.readouterr().out)


def test_recurring_goal_never_an_eligible_candidate(monkeypatch, capsys):
    out = _run_main(monkeypatch, capsys, _recurring_shape(recurring=True))
    assert out["candidates"] == [], (
        f"a recurring goal must never be a supersession candidate: {out['candidates']}")
    assert out["eligible"] == 0, (
        f"a recurring goal must not even count as eligible, got {out['eligible']}")
    reasons = [d.get("reason", "") for d in out["details"]
               if d.get("goal_id") == "g-350-04"]
    assert any("recurring" in r for r in reasons), (
        f"expected an explicit recurring skip reason, got {out['details']}")
    # The skip must be attributed to recurrence, NOT to some other guard that
    # happens to be holding — that ambiguity is what hid the live instance.
    assert not any("insufficient sibling" in r for r in reasons), reasons


def test_positive_control_same_shape_without_recurring_is_a_candidate(
        monkeypatch, capsys):
    """The discriminating half: proves the shape DOES match when not recurring.

    Without this, the assertion above passes vacuously for any shape the sweep
    never matched in the first place — and a vacuous pass on an exclusion test
    is indistinguishable from a working exclusion.
    """
    out = _run_main(monkeypatch, capsys, _recurring_shape(recurring=False))
    ids = [c["goal_id"] for c in out["candidates"]]
    assert ids == ["g-350-04"], (
        f"non-recurring parent of the same shape MUST be a candidate, got {out}")
    assert out["eligible"] == 1, out
