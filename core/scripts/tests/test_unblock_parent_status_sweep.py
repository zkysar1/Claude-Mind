"""test_unblock_parent_status_sweep.py — regression test for  / rb-908.

Asserts that unblock-parent-status-sweep.py's helper functions correctly
identify the g-250-73 canonical incident shape AND reject false-positive
cases that would have leaked through a less-conservative heuristic.

Cases covered:
  1. parent_id parser: origin_signal "unblock:g-250-69" → g-250-69
  2. parent_id parser: title "Unblock: behavior for g-250-69" → g-250-69
  3. parent_id parser: discovered_by "g-250-69" → g-250-69
  4. parent_id parser: nothing parseable → None
  5. Unblock title matcher: "Unblock: foo" yes, "Investigate: foo" no,
     " Unblock : foo" yes (whitespace tolerant), "" no
  6. Idempotency check: outcome_note already starts with parent-resolved
     phrase → considered already swept
  7. Terminal-state set: skipped/completed/superseded/archived are sweep
     targets; pending/in-progress are NOT.

Pattern: same importlib + sys.path shape as test_parent_supersession_sweep.py.
unblock-parent-status-sweep.py uses a hyphenated filename so we load it via
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
    """Load unblock-parent-status-sweep.py via importlib."""
    spec = importlib.util.spec_from_file_location(
        "unblock_parent_status_sweep_mod",
        CORE_SCRIPTS / "unblock-parent-status-sweep.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(
            "could not load spec for unblock-parent-status-sweep.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parent_id_from_origin_signal_canonical_shape():
    """rb-908 canonical: capability-gate._build_suggestion emits
    origin_signal='unblock:<parent-id>' verbatim."""
    mod = _import_sweep()
    g = {
        "id": "g-250-73",
        "title": "Unblock: behavior for g-250-69",
        "origin_signal": "unblock:g-250-69",
    }
    assert mod._parse_parent_id(g) == "g-250-69"


def test_parent_id_from_title_for_pattern():
    """Layer D canonical title 'Unblock: <verb> for <parent-id>'."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: deploy for g-115-87",
        # No origin_signal — fallback to title regex
    }
    assert mod._parse_parent_id(g) == "g-115-87"


def test_parent_id_origin_signal_beats_title():
    """When both origin_signal and title contain goal-ids, origin_signal
    takes precedence (priority order — origin_signal is most authoritative
    since it's what capability-gate emits)."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: thing for g-999-99",  # different from origin_signal
        "origin_signal": "unblock:g-250-69",
    }
    # origin_signal wins
    assert mod._parse_parent_id(g) == "g-250-69"


def test_parent_id_from_discovered_by_fallback():
    """If origin_signal and title both lack a goal-id, fall back to
    discovered_by field."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",  # no 'for g-NNN-NN'
        "origin_signal": "unblock:something-narrative",  # not a goal-id form
        "discovered_by": "g-250-69",
    }
    assert mod._parse_parent_id(g) == "g-250-69"


def test_parent_id_unparseable_returns_none():
    """No goal-id anywhere → None (the goal won't be considered for
    sweeping). Conservative — we never guess a parent."""
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",
        "origin_signal": "unblock:narrative-only",
        "discovered_by": "user_directive",  # not a goal-id pattern
    }
    assert mod._parse_parent_id(g) is None


def test_origin_signal_must_match_exact_form():
    """origin_signal regex requires 'unblock:<g-NNN-NN>' exactly — does
    not consume narrative suffixes like 'unblock:g-115-129-precondition'."""
    mod = _import_sweep()
    # narrative suffix after the goal-id — NOT exact form
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",  # title path also no match
        "origin_signal": "unblock:g-115-129-precondition",
    }
    # origin_signal regex anchored at $ rejects suffix — falls through
    assert mod._parse_parent_id(g) is None


def test_unblock_title_matcher_accepts_canonical_and_whitespace():
    """Title must start with literal 'Unblock:' (case-insens, ws-tolerant)."""
    mod = _import_sweep()
    assert mod._is_unblock_goal({"title": "Unblock: foo"}) is True
    assert mod._is_unblock_goal({"title": " Unblock : foo"}) is True
    assert mod._is_unblock_goal({"title": "unblock: foo"}) is True


def test_unblock_title_matcher_rejects_other_prefixes():
    """Investigate/Idea/Apply/Maintain/Recurring titles are NOT Layer D
    auto-Unblocks — even if they happen to use 'unblock:' in origin_signal
    for unrelated reasons (g-249-06 / g-250-77 false-positive shape)."""
    mod = _import_sweep()
    assert mod._is_unblock_goal({"title": "Investigate: foo"}) is False
    assert mod._is_unblock_goal({"title": "Idea: foo"}) is False
    assert mod._is_unblock_goal({"title": "Apply: foo"}) is False
    assert mod._is_unblock_goal({"title": "Maintain: foo"}) is False
    assert mod._is_unblock_goal({"title": "Recurring: foo"}) is False
    assert mod._is_unblock_goal({"title": ""}) is False
    assert mod._is_unblock_goal({}) is False


def test_idempotency_already_swept_detection():
    """Once outcome_note carries the parent-resolved phrase, subsequent
    sweeps must not re-mark the goal — prevents double-write loops."""
    mod = _import_sweep()
    already = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "origin_signal": "unblock:g-002",
        "outcome_note": ("parent resolved without action needed "
                         "(parent_id=g-002, parent.status=skipped)"),
    }
    assert mod._is_already_swept(already) is True
    fresh = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "origin_signal": "unblock:g-002",
        "outcome_note": "",
    }
    assert mod._is_already_swept(fresh) is False
    # No outcome_note at all → not swept
    bare = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
    }
    assert mod._is_already_swept(bare) is False


def test_terminal_states_set():
    """The terminal states that indicate 'no Unblock action is needed' are
    locked to the SSOT aspirations.TERMINAL_GOAL_STATUSES: completed, skipped,
    expired, decomposed, superseded. g-303-21 (zeta audit D1) removed the
    bogus 'archived' (never a valid goal status) and added expired/decomposed,
    which were silently dropping stale Unblock work on expired/decomposed
    parents. Pending/in-progress/blocked must NOT be in this set or the sweep
    would clear actively-working parents."""
    mod = _import_sweep()
    assert "skipped" in mod.TERMINAL_STATES
    assert "completed" in mod.TERMINAL_STATES
    assert "superseded" in mod.TERMINAL_STATES
    assert "expired" in mod.TERMINAL_STATES
    assert "decomposed" in mod.TERMINAL_STATES
    assert "archived" not in mod.TERMINAL_STATES  # not a valid goal status ()
    assert "pending" not in mod.TERMINAL_STATES
    assert "in-progress" not in mod.TERMINAL_STATES
    assert "blocked" not in mod.TERMINAL_STATES


def test_canonical_incident_g_250_73_shape_recognized():
    """End-to-end shape recognition: an Unblock with exactly the 
    field set should parse to parent g-250-69 AND match the Unblock title
    AND not be considered already-swept."""
    mod = _import_sweep()
    g = {
        "id": "g-250-73",
        "title": "Unblock: behavior for g-250-69",
        "status": "pending",
        "origin_signal": "unblock:g-250-69",
        "tags": ["unblock", "defer-gate-routed", "framework-maintenance"],
        "outcome_note": "",
        "created_at": "2026-05-13T09:45:14",
    }
    assert mod._is_unblock_goal(g) is True
    assert mod._parse_parent_id(g) == "g-250-69"
    assert mod._is_already_swept(g) is False


def test_status_index_builder():
    """_build_status_index returns {goal_id: status} across all
    aspirations regardless of source/aspiration boundary."""
    mod = _import_sweep()
    all_asps = [
        ({"id": "asp-1", "goals": [
            {"id": "g-1", "status": "pending"},
            {"id": "g-2", "status": "completed"},
        ]}, "world"),
        ({"id": "asp-2", "goals": [
            {"id": "g-3", "status": "skipped"},
            {"id": "g-4"},  # status missing
        ]}, "agent"),
    ]
    idx = mod._build_status_index(all_asps)
    assert idx == {"g-1": "pending", "g-2": "completed",
                   "g-3": "skipped", "g-4": None}


def test_rb3887_provenance_created_after_parent_completion_guarded():
    """rb-3887 /  canonical FP: an Unblock whose ONLY parent link
    is discovered_by (sq-013 provenance) and whose created_at POSTDATES the
    parent's completion must be guarded — it was never waiting on that
    parent (g-115-2530/2531 shape: auto-skipped within one iteration)."""
    mod = _import_sweep()
    g = {
        "id": "g-115-2530",
        "title": "Unblock: commit+push perception-verticle-scaffolding SKILL.md",
        "status": "pending",
        "discovered_by": "g-307-62",
        "created_at": "2026-07-17T20:00:00",
    }
    ts_idx = {"g-307-62": "2026-07-17T18:00:00"}  # parent completed FIRST
    assert mod._parse_parent_id(g) == "g-307-62"
    reason = mod._provenance_fp_guard(g, "g-307-62", ts_idx)
    assert reason is not None and "rb-3887" in reason


def test_rb3887_legacy_wait_created_before_parent_completion_sweeps():
    """guard-958 recall control (legacy population): a discovered_by-only
    Unblock created BEFORE the parent completed is a genuine wait — the
    guard must return None so the sweep still covers it."""
    mod = _import_sweep()
    g = {
        "id": "g-250-90",
        "title": "Unblock: restore access",  # no 'for <g-id>' form
        "status": "pending",
        "discovered_by": "g-250-69",
        "created_at": "2026-05-13T09:00:00",
    }
    ts_idx = {"g-250-69": "2026-05-13T09:46:25"}  # parent completed AFTER
    assert mod._provenance_fp_guard(g, "g-250-69", ts_idx) is None


def test_rb3887_missing_timestamps_conservative_guard():
    """Missing/unparseable timestamps cannot PROVE a genuine wait — the
    guard fires (conservative: the FP direction auto-skips live work; the
    miss direction is benign). Covers absent parent entry (archived-parent
    default) and absent created_at."""
    mod = _import_sweep()
    g = {
        "id": "g-1",
        "title": "Unblock: restore access",
        "discovered_by": "g-2",
        "created_at": "2026-07-17T20:00:00",
    }
    assert mod._provenance_fp_guard(g, "g-2", {}) is not None  # no parent ts
    g_no_created = {
        "id": "g-1",
        "title": "Unblock: restore access",
        "discovered_by": "g-2",
    }
    ts_idx = {"g-2": "2026-07-17T18:00:00"}
    assert mod._provenance_fp_guard(g_no_created, "g-2", ts_idx) is not None


def test_g115_2674_origin_signal_form_guarded_when_created_after_parent():
    """REVERSED by  (2026-07-19). This test previously asserted the
    OPPOSITE — that a priority-1/2 link is NEVER provenance-guarded, on the
    premise that "Layer D emits those at defer time by construction".

    That premise is false. `origin_signal: unblock:<parent>` is also the
    DOCUMENTED convention for filing a follow-up Unblock BY HAND, so the
    field cannot distinguish a Layer-D auto-conversion from an
    agent-authored follow-up. The exemption was a hole and it fired: 7 live
    goals auto-skipped fleet-wide, including two HIGH goals killed within
    minutes of filing (g-312-09, g-318-63) and a HIGH heartbeat-writer fix
    (g-115-2182) that sat dead 5 days.

    The timestamp test alone separates the two shapes, so the guard now
    applies at ALL link priorities. See the companion test below for the
    Layer-D no-regression proof."""
    mod = _import_sweep()
    g = {
        "id": "g-250-73",
        "title": "Unblock: behavior for g-250-69",
        "origin_signal": "unblock:g-250-69",
        "discovered_by": "g-250-69",
        "created_at": "2026-05-14T00:00:00",
    }
    ts_idx = {"g-250-69": "2026-05-13T09:46:25"}  # parent completed BEFORE
    assert "rb-3887" in mod._provenance_fp_guard(g, "g-250-69", ts_idx)
    # Same for the title 'for <g-id>' form without origin_signal:
    g_title = {
        "id": "g-250-74",
        "title": "Unblock: behavior for g-250-69",
        "discovered_by": "g-250-69",
        "created_at": "2026-05-14T00:00:00",
    }
    assert "rb-3887" in mod._provenance_fp_guard(g_title, "g-250-69", ts_idx)


def test_g115_2674_layer_d_defer_time_unblock_still_sweeps():
    """No-regression proof for the  widening: a GENUINE Layer-D
    Unblock is emitted at DEFER time, while its parent is still pending, so
    it is created BEFORE the parent ever completes. `created < done` holds,
    the guard returns None, and the sweep proceeds exactly as it did before
    the widening. Widening therefore costs real Layer-D goals nothing — it
    only closes the created-at-or-after-completion hole."""
    mod = _import_sweep()
    layer_d = {
        "id": "g-250-80",
        "title": "Unblock: deploy for g-250-69",
        "origin_signal": "unblock:g-250-69",
        "discovered_by": "g-250-69",
        "created_at": "2026-05-10T08:00:00",   # filed at defer time
    }
    ts_idx = {"g-250-69": "2026-05-13T09:46:25"}  # parent completed LATER
    assert mod._provenance_fp_guard(layer_d, "g-250-69", ts_idx) is None


def test_rb3887_aware_offset_timestamp_normalized_no_crash():
    """Fresh-eyes-code finding ( dispatch): an offset-aware stamp
    (+00:00 — the fleet is TZ-split, rb-3741) meeting a naive one at the
    `created < done` comparison raised TypeError and crashed the whole
    sweep. _parse_ts must normalize to naive-local (guard-982 pattern).
    Margins ≥38h so assertions hold under ANY box timezone (±14h max)."""
    mod = _import_sweep()
    # Normalization invariant: parsed aware stamp comes back naive.
    parsed = mod._parse_ts("2026-07-17T20:00:00+00:00")
    assert parsed is not None and parsed.tzinfo is None
    assert mod._parse_ts("2026-07-17T20:00:00Z").tzinfo is None
    assert mod._parse_ts("2026-07-17T20:00:00").tzinfo is None
    base = {"id": "g-1", "title": "Unblock: restore access",
            "discovered_by": "g-2"}
    ts_idx = {"g-2": "2026-07-17T18:00:00"}  # naive parent completion
    # Aware created FAR BEFORE naive done → genuine wait, sweeps (None).
    g_before = dict(base, created_at="2026-07-15T00:00:00+00:00")
    assert mod._provenance_fp_guard(g_before, "g-2", ts_idx) is None
    # Aware created FAR AFTER naive done → provenance, guarded.
    g_after = dict(base, created_at="2026-07-19T00:00:00+00:00")
    assert "rb-3887" in mod._provenance_fp_guard(g_after, "g-2", ts_idx)
    # Aware DONE vs naive created (the mirror mix) → no crash either.
    aware_idx = {"g-2": "2026-07-17T18:00:00+00:00"}
    g_naive = dict(base, created_at="2026-07-15T00:00:00")
    assert mod._provenance_fp_guard(g_naive, "g-2", aware_idx) is None


def test_completed_ts_index_builder():
    """_build_completed_ts_index maps goal_id → completed_at, falling back
    to completed_date, None when neither present."""
    mod = _import_sweep()
    all_asps = [
        ({"id": "asp-1", "goals": [
            {"id": "g-1", "completed_at": "2026-07-17T18:00:00"},
            {"id": "g-2", "completed_date": "2026-07-16"},
            {"id": "g-3"},
        ]}, "world"),
    ]
    idx = mod._build_completed_ts_index(all_asps)
    assert idx == {"g-1": "2026-07-17T18:00:00",
                   "g-2": "2026-07-16", "g-3": None}


if __name__ == "__main__":
    test_parent_id_from_origin_signal_canonical_shape()
    test_parent_id_from_title_for_pattern()
    test_parent_id_origin_signal_beats_title()
    test_parent_id_from_discovered_by_fallback()
    test_parent_id_unparseable_returns_none()
    test_origin_signal_must_match_exact_form()
    test_unblock_title_matcher_accepts_canonical_and_whitespace()
    test_unblock_title_matcher_rejects_other_prefixes()
    test_idempotency_already_swept_detection()
    test_terminal_states_set()
    test_canonical_incident_g_250_73_shape_recognized()
    test_status_index_builder()
    test_rb3887_provenance_created_after_parent_completion_guarded()
    test_rb3887_legacy_wait_created_before_parent_completion_sweeps()
    test_rb3887_missing_timestamps_conservative_guard()
    test_rb3887_origin_signal_form_never_guarded()
    test_rb3887_aware_offset_timestamp_normalized_no_crash()
    test_completed_ts_index_builder()
    print("All 18 tests passed.")


def test_g115_2681_close_sequence_window_guards_followup_filed_during_parent_close():
    """ (2026-07-19) — boundary fix for the  guard.

    g-115-2674 tested `created < parent_completed` and treated ANY earlier
    creation as a "genuine wait". Wrong at the margin: an Unblock filed
    DURING its parent's close sequence (Phase 4 surfaces a finding -> agent
    files the follow-up -> verify/state-update/learning-gate then stamp the
    parent terminal) is created SECONDS before the parent completes and is a
    FOLLOW-UP, not a wait.

    Measured FPs that the bare test re-swept even AFTER g-115-2674 landed —
    each description literally opens "MEASURED during <parent>":
      g-318-63   28s lead   (2 declared ohs-trend fields never populated, 0/32)
      g-350-21   93s lead   (client deploy leg; server leg already shipped)
      g-115-2533 97s lead   (commit+push a re-materialized, still-gitignored SKILL.md)
    """
    mod = _import_sweep()
    # 28s lead — the tightest observed real FP.
    g = {
        "id": "g-318-63",
        "title": "Unblock: ohs-trend fields never populated",
        "origin_signal": "unblock:g-318-62",
        "discovered_by": "g-318-62",
        "created_at": "2026-07-19T09:13:45",
    }
    ts_idx = {"g-318-62": "2026-07-19T09:14:13"}
    reason = mod._provenance_fp_guard(g, "g-318-62", ts_idx)
    assert reason is not None, "follow-up filed 28s before parent close must be guarded"
    assert "close-sequence window" in reason


def test_g115_2681_genuine_wait_outside_window_still_sweeps():
    """REACH-PRESERVED proof — the assertion that matters for this fix.

    The risk of a tolerance window is over-guarding the sweep into a no-op.
    A genuine Layer-D Unblock is filed at DEFER time while the parent is
    still pending — typically hours-to-days ahead, far outside the window —
    so it must STILL sweep. This pins that the window closed only the
    close-sequence margin, not the sweep itself."""
    mod = _import_sweep()
    g = {
        "id": "g-999-01",
        "title": "Unblock: deploy for g-999-00",
        "origin_signal": "unblock:g-999-00",
        "discovered_by": "g-999-00",
        "created_at": "2026-07-01T00:00:00",
    }
    # Parent completed 3 days later — a real wait, nowhere near the window.
    ts_idx = {"g-999-00": "2026-07-04T00:00:00"}
    assert mod._provenance_fp_guard(g, "g-999-00", ts_idx) is None

    # Boundary: just OUTSIDE the 900s window (901s lead) must still sweep.
    g_edge = dict(g, created_at="2026-07-01T00:00:00")
    ts_edge = {"g-999-00": "2026-07-01T00:15:01"}
    assert mod._provenance_fp_guard(g_edge, "g-999-00", ts_edge) is None

    # Boundary: just INSIDE (899s lead) must be guarded.
    ts_in = {"g-999-00": "2026-07-01T00:14:59"}
    assert mod._provenance_fp_guard(g_edge, "g-999-00", ts_in) is not None
