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


def test_narrative_suffix_is_not_absorbed_into_the_id():
    """A narrative suffix must not be consumed INTO the goal-id.

    ASSERTION FLIPPED IN g-115-5647, deliberately. This test previously
    asserted that 'unblock:g-115-129-precondition' parses to None, on the
    reasoning that the end-anchored regex "rejects the suffix". But rejecting
    the whole signal is not the same as not absorbing the suffix, and the
    live corpus is full of exactly this shape — 'unblock:g-335-983-unshipped-
    duplicate-prs' is a real goal whose parent the sweep could not find. The
    old assertion pinned the narrowness that WAS the defect.

    The property genuinely worth protecting is that the id ends where the
    digits end: g-115-129, never g-115-129-precondition. That is what the
    embedded pattern guarantees (\\d+ cannot match a letter) and what this
    test now checks.
    """
    mod = _import_sweep()
    g = {
        "id": "g-001",
        "title": "Unblock: do the thing",   # no id in the title path
        "origin_signal": "unblock:g-115-129-precondition",
    }
    assert mod._parse_parent_id(g) == "g-115-129"


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
    """Once a goal is FULLY swept — note AND terminal status — subsequent
    sweeps must not re-mark it; prevents double-write loops.

    The `already` fixture gained an explicit terminal status in g-115-5097.
    Before that it carried the note and no status at all, which pinned the
    defect by omission: it asserted that a note ALONE proves a completed
    sweep. It does not — see test_partial_write_is_not_already_swept.
    """
    mod = _import_sweep()
    already = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "origin_signal": "unblock:g-002",
        "status": "skipped",
        "outcome_note": ("parent resolved without action needed "
                         "(parent_id=g-002, parent.status=skipped)"),
    }
    assert mod._is_already_swept(already) is True
    fresh = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "origin_signal": "unblock:g-002",
        "status": "pending",
        "outcome_note": "",
    }
    assert mod._is_already_swept(fresh) is False
    # No outcome_note at all → not swept
    bare = {
        "id": "g-001",
        "title": "Unblock: x for g-002",
        "status": "pending",
    }
    assert mod._is_already_swept(bare) is False


def test_partial_write_is_not_already_swept():
    """ — the load-bearing pin. POSITIVE CONTROL: this fixture is
    RED against the pre-fix guard (`note.startswith(...)` alone returns True).

    _mark_skipped writes note then status as two non-atomic daemon calls. If
    write 2 fails, the goal carries the sweep's note while status is STILL
    pending. Keying dedup on the note alone made that state permanently
    self-sealing: the sweep skipped it on every later run, so its own partial
    success blocked its own repair — invisibly, because the sweep reported the
    goal as already-swept rather than as a failure.

    Requiring a terminal status means this state re-qualifies and self-heals.
    """
    mod = _import_sweep()
    note = ("parent resolved without action needed "
            "(parent_id=g-002, parent.status=skipped)")
    for stranded_status in ("pending", "in-progress"):
        stranded = {
            "id": "g-001",
            "title": "Unblock: x for g-002",
            "origin_signal": "unblock:g-002",
            "status": stranded_status,
            "outcome_note": note,
        }
        assert mod._is_already_swept(stranded) is False, (
            f"note-bearing goal with status={stranded_status!r} must re-qualify "
            f"for retry, not be treated as swept (g-115-5097)"
        )
    # Every terminal status counts as swept, not just the one _mark_skipped
    # writes — a goal closed some other way after the note landed is done too.
    for terminal in sorted(mod.TERMINAL_STATES):
        done = {
            "id": "g-001",
            "title": "Unblock: x for g-002",
            "origin_signal": "unblock:g-002",
            "status": terminal,
            "outcome_note": note,
        }
        assert mod._is_already_swept(done) is True, terminal


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


# ---------------------------------------------------------------------------
# guard-1890 / guard-2390 — archive resolution + the dead-sentinel regression
#
# The defect these pin (measured 2026-08-13, alpha, hostname cc-04, uname -r
# 6.8.0-137-generic): the sweep resolved a parent with
# `status_idx.get(parent_id, "archived")` — a SENTINEL meaning "absent from the
# active scan", which the docstring called "the supersession signal". 
# then removed "archived" from TERMINAL_STATES as a bogus goal status. Both
# changes are individually correct; together they killed the branch, because the
# sentinel could no longer pass its own terminal test. The sweep then reported
# `parent not in terminal state (parent.status=archived)` about goals carrying no
# such status — a sentinel reaching a human-readable audit record (guard-2390).
#
# Neither existing test could catch it: test_terminal_states_set pins the SET and
# test_status_index_builder pins the ACTIVE index. Nothing covered their
# INTERACTION, which is the whole defect. That is why these are behavioural pins
# on the resolution helpers rather than another constant assertion.
# ---------------------------------------------------------------------------


def test_archived_id_set_keys_on_membership_not_frozen_status():
    """An archived aspiration's goals are dead regardless of the status they
    froze with — its ASPIRATION being archived is the signal, not the goal's
    own stale status field. A goal frozen as `pending` inside an archived
    aspiration must still register as archived, or the sweep re-derives the
    terminal question from a field that stopped being maintained."""
    mod = _import_sweep()
    archived = [
        ({"id": "asp-90", "goals": [
            {"id": "g-90-01", "status": "completed"},
            {"id": "g-90-02", "status": "pending"},    # froze mid-flight
            {"id": "g-90-03"},                          # no status at all
        ]}, "world"),
        ({"id": "asp-91", "goals": [{"id": "g-91-01", "status": "blocked"}]},
         "agent"),
    ]
    ids = mod._build_archived_id_set(archived)
    assert ids == {"g-90-01", "g-90-02", "g-90-03", "g-91-01"}


def test_archived_id_set_empty_archive_is_valid_not_degraded():
    """A fresh world has an empty archive. That is a valid state, NOT a
    failure — treating it as degraded would make every clean world look
    broken."""
    mod = _import_sweep()
    assert mod._build_archived_id_set([]) == set()


def test_archive_read_degrades_while_active_read_stays_fatal():
    """The asymmetry is the safety property (guard-1890, guard-383).

    Losing the ARCHIVE falls back to pre-fix behaviour: an archived parent
    reads as unresolvable, so the Unblock is left alone — a false-NEGATIVE,
    the sweep does less, visibly. Losing an ACTIVE source would make a
    still-pending parent look absent and could sweep a LIVE Unblock, so it
    must stay fatal. A future refactor that makes both degrade would silently
    arm exactly that.
    """
    mod = _import_sweep()

    def _boom(*a, **kw):
        raise mod._rt.RtError("simulated daemon failure")

    orig = mod._rt.aspirations_read
    try:
        mod._rt.aspirations_read = _boom
        # ARCHIVE: degrades to None, never raises, never exits.
        assert mod._read_archived_aspirations("world") is None
        # ACTIVE: guard-383 fatal — a silent [] would poison the merged
        # world+agent aggregate with a complete-looking lie.
        try:
            mod._read_aspirations("world")
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError(
                "_read_aspirations must exit(1) on a source read failure; "
                "degrading it would let a live Unblock be swept against an "
                "apparently-absent parent")
    finally:
        mod._rt.aspirations_read = orig


def test_absence_sentinel_never_collides_with_terminal_states():
    """The regression pin for the dead branch itself.

    `archived` is deliberately NOT a member of TERMINAL_STATES (g-303-21), so
    resolving an absent parent to that STRING can never mark it terminal. The
    sweep must therefore decide absence by ARCHIVE MEMBERSHIP, and the two
    mechanisms must stay distinct. If a future edit re-adds "archived" to
    TERMINAL_STATES to "fix" an absent parent, this fails and points at the
    membership path instead — the parity test would fail too, from the other
    side.
    """
    mod = _import_sweep()
    assert "archived" not in mod.TERMINAL_STATES
    # The resolution path exists and is membership-based, not string-based.
    assert callable(mod._build_archived_id_set)
    assert callable(mod._read_archived_aspirations)
    # An absent id resolves to None from the ACTIVE index — never to a string
    # that could be interpolated into a reason as though it were a real status
    # (guard-2390).
    idx = mod._build_status_index([({"id": "asp-1", "goals": [
        {"id": "g-1", "status": "pending"}]}, "world")])
    assert idx.get("g-does-not-exist") is None


# --- : successor/residual-scope guard ------------------------------


def test_successor_guard_live_incident_shape_guarded():
    """The  shape: description asserts 'case A - successor
    preserving <parent> sanctioned scope'. Created ~4h BEFORE the parent
    completed, so every timestamp guard passes it — the TEXT is the only
    discriminator. Must return a reason, never None."""
    mod = _import_sweep()
    g = {
        "id": "g-350-215",
        "title": "Unblock: DEV host infra (ReplicaServer + PlayerProfiles)",
        "origin_signal": "unblock:g-350-202",
        "description": ("filed by alpha worker Body on cc-07 — case A - "
                        "successor preserving g-350-202 sanctioned scope: "
                        "that goal's product outcome is the carved tree "
                        "LOADS in DEV, and the require rewrite (PR #6) "
                        "delivers only the self-consistency half"),
    }
    reason = mod._successor_scope_guard(g)
    assert reason is not None
    assert "g-115-6223" in reason
    assert "PRECONDITION" in reason


def test_successor_guard_each_marker_word_fires():
    mod = _import_sweep()
    for marker in ("successor", "Successor Unblock carrying scope",
                   "residual scope from the parent",
                   "the remainder of the parent's outcome",
                   "PR #6 delivers only the self-consistency half",
                   "case A - preserving sanctioned scope"):
        g = {"title": "Unblock: x for g-1-1", "description": marker,
             "origin_signal": ""}
        assert mod._successor_scope_guard(g) is not None, marker


def test_successor_guard_matches_title_and_origin_signal_too():
    mod = _import_sweep()
    by_title = {"title": "Unblock: successor for g-1-1", "description": "",
                "origin_signal": ""}
    assert "title" in mod._successor_scope_guard(by_title)
    by_os = {"title": "Unblock: x for g-1-1", "description": "",
             "origin_signal": "unblock:residual-of-g-1-1"}
    assert "origin_signal" in mod._successor_scope_guard(by_os)


def test_successor_guard_plain_wait_unblock_still_sweepable():
    """The sweep's whole population must not be guarded away: a plain
    Layer-D defer-time Unblock with wait semantics returns None."""
    mod = _import_sweep()
    plain = {
        "title": "Unblock: deploy access for g-115-9999",
        "origin_signal": "unblock:g-115-9999",
        "description": "Filed at defer time; waiting on parent credential grant.",
    }
    assert mod._successor_scope_guard(plain) is None


def test_successor_guard_case_a_is_case_sensitive():
    """Under IGNORECASE the 'case A' marker would match the common prose
    shape 'in case a goal…' — the alternative is deliberately split out
    case-sensitive. Lowercase prose must NOT trip the guard."""
    mod = _import_sweep()
    prose = {"title": "Unblock: retry for g-1-1",
             "description": "re-probe in case a partner is idle; in case a "
                            "goal stalls the selector re-ranks",
             "origin_signal": ""}
    assert mod._successor_scope_guard(prose) is None
    filed = {"title": "Unblock: retry for g-1-1",
             "description": "filed by echo worker Body, case A, carrying the "
                            "parent's sanctioned scope",
             "origin_signal": ""}
    assert mod._successor_scope_guard(filed) is not None


def test_successor_guard_ignores_outcome_note():
    """outcome_note is written by this sweep itself (and by re-open
    narratives recording the oscillation), so successor words THERE must
    not key the guard — only description/title/origin_signal count."""
    mod = _import_sweep()
    g = {"title": "Unblock: x for g-1-1", "description": "", "origin_signal": "",
         "outcome_note": "re-opened: this successor was wrongly swept"}
    assert mod._successor_scope_guard(g) is None


# ---------------------------------------------------------------------------
# MERGE NOTE (2026-08-14, cc-08). The two blocks below are the SAME guard's
# suites, written independently on two Bodies ( and ) from
# the same live instance. BOTH are kept: the implementations were unified into
# _successor_marker_guard and _successor_scope_guard is now an alias of it, so
# every pin below exercises the one surviving function. Deleting either block to
# "clean up the merge" would discard measured coverage — the g-6223 block owns
# the multi-field (title/origin_signal) and outcome_note-exclusion pins, the
# g-6252 block owns the English-false-positive and case-letter-variant pins, and
# neither is a superset of the other.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  — the SUCCESSOR guard. The sweep's premise ("parent terminal =>
# the Unblock is moot") inverts for a successor, whose whole reason for
# existing is that the parent completed leaving residual scope.
#
# All six records below are the LIVE shapes measured on 2026-08-14 (alpha,
# hostname cc-07), not invented fixtures.
# ---------------------------------------------------------------------------

# The real record, verbatim in the fields the guards read.
_G350215 = {
    "id": "g-350-215",
    "title": ("Unblock: provision the DEV-place host infra the carved GameSystem "
              "tree requires to load (ReplicaServer, PlayerProfiles, ProfileStore, "
              "Signal, Types, Utils.Net, AyoTypes)"),
    "status": "pending",
    "created_at": "2026-08-14T03:35:05",
    "origin_signal": ("unblock: g-350-202 require rewrite (PR #6) delivers tree "
                      "self-consistency but load in DEV needs host infra measured "
                      "absent (foxtrot probe 2026-08-14)"),
    "description": ("FILED BY alpha WORKER BODY ON cc-08 (case A - successor "
                    "preserving g-350-202 sanctioned scope: that goal's product "
                    "outcome is 'the carved tree LOADS in DEV')"),
}
_G350202_DONE = {"g-350-202": "2026-08-14T07:40:03"}


def test_g115_6252_successor_marker_guards_the_live_case_a_shape():
    """The canonical instance.  declares itself a successor in its
    own description and was nonetheless marked with the sweep's note
    "parent resolved without action needed (parent_id=g-350-202,
    parent.status=completed)" — a record that contradicts itself on its face.
    """
    mod = _import_sweep()
    # The parent link resolves (so the goal genuinely reaches the guards).
    assert mod._parse_parent_id(_G350215) == "g-350-202"
    reason = mod._successor_marker_guard(_G350215)
    assert reason is not None
    assert "SUCCESSOR" in reason
    assert "g-115-6252" in reason


def test_g115_6252_temporal_guard_reads_the_successor_shape_backwards():
    """THE LOAD-BEARING PIN — why this is a SECOND guard, not a tuning.

    `_provenance_fp_guard` asks whether the Unblock was created AT/AFTER the
    parent completed. A successor is filed DURING the parent's close, hours
    BEFORE the completion stamp lands, so that guard clears the sweep with the
    reason "genuine wait: Unblock long predates parent completion". Measured
    lead on the live pair: 07:40:03 - 03:35:05 = 14698s against
    CLOSE_SEQUENCE_WINDOW_S=900.

    The guard therefore grows MORE confident the longer the parent took —
    exactly inverted. If someone later merges the two guards or widens
    CLOSE_SEQUENCE_WINDOW_S to "cover this case", this test fails and says why.
    """
    mod = _import_sweep()
    assert mod._provenance_fp_guard(_G350215, "g-350-202", _G350202_DONE) is None
    lead = (mod._parse_ts(_G350202_DONE["g-350-202"])
            - mod._parse_ts(_G350215["created_at"])).total_seconds()
    assert lead > mod.CLOSE_SEQUENCE_WINDOW_S
    # ...and the successor guard is what actually catches it.
    assert mod._successor_marker_guard(_G350215) is not None


def test_g115_6252_second_live_instance_uses_different_wording():
    """, the OTHER real successor in the live Unblock population,
    writes "worker-loop ruling Case A" — capital C. A predicate anchored on
    lowercase `case` alone misses it, which is how it first escaped."""
    mod = _import_sweep()
    g = {
        "id": "g-115-6161",
        "title": ("Unblock: grant lambda:UpdateFunctionConfiguration on "
                  "RotateAPIKey+RevokeAPIKey to ayoai-fleet-agent"),
        "status": "pending",
        "description": ("Filed by alpha worker Body on cc-07 (worker-loop ruling "
                        "Case A — carries the unfinished IAM leg of "
                        "g-115-6080/g-115-6078)."),
    }
    assert mod._successor_marker_guard(g) is not None


def test_g115_6252_uppercase_a_is_the_discriminator_against_english():
    """`case a` is ordinary English and appears throughout goal descriptions —
    "in which case a", "worst case a", "special-case a single filename".
    Measured: case-insensitive matching pulled 7 such English hits into a
    45-hit whole-population set. The uppercase A is what separates the
    ruling's case letter from the article. Do not "tidy" this to IGNORECASE.
    """
    mod = _import_sweep()
    for english in (
        "Either (a) it is load-bearing, in which case a 13% population rate is "
        "the defect and the close path should require it.",
        "This cannot introduce a false positive — worst case a genuinely-stale "
        "refusal re-surfaces late.",
        "Do NOT special-case a single filename — the registry scope is the "
        "finding.",
    ):
        g = {"id": "g-x", "title": "Unblock: something", "description": english}
        assert mod._successor_marker_guard(g) is None, english[:48]


def test_g115_6252_case_letter_variants_all_match():
    """The ruling marker is written case/Case/CASE across the live corpus; only
    the `A` is pinned, so all three spellings must reach the guard."""
    mod = _import_sweep()
    for spelling in ("case A", "Case A", "CASE A"):
        g = {"id": "g-x", "title": "Unblock: x",
             "description": f"Filed by alpha worker Body on cc-07 ({spelling} "
                            f"under the g-306-250 ruling)."}
        assert mod._successor_marker_guard(g) is not None, spelling


def test_g115_6252_ordinary_blocked_unblock_still_sweeps():
    """RECALL CONTROL — the guard must not quietly turn the sweep into a no-op.

    Measured on the live corpus: 2 of 32 Unblock-titled non-terminal goals carry
    a successor marker, so 30 of 32 stay sweepable. These are real titles from
    that unflagged 30.
    """
    mod = _import_sweep()
    for gid, desc in (
        ("g-115-5799", "Reap the two wedged stdin-writer processes on cc-03 "
                       "(pids 660157, 3102967) — cross-box, needs the owner."),
        ("g-335-944", "Merge PR #130 (g-335-936 PDF font-embedding assertion) — "
                      "blocked only by a GitHub required-check that never ran."),
        ("g-328-36", "Run own-cloud bootstrap pull on the affected Windows box — "
                     "heal the 5-file knowledge gap."),
    ):
        g = {"id": gid, "title": "Unblock: x", "description": desc}
        assert mod._successor_marker_guard(g) is None, gid


def test_g115_6252_each_secondary_token_is_independently_pinned():
    """Every token in the union carries its own pin, or it is not protected.

    Found by mutation-testing the pins themselves: deleting `\\bsuccessor\\b`
    from the pattern turned ZERO tests red, because on the current corpus every
    real successor also carries the ruling's case letter — so the token buys no
    measurable recall TODAY and reads as removable. It is not: the case letter
    exists only because the worker-loop g-306-250 ruling obliges a WORKER to
    stamp it (see _successor_marker_guard's KNOWN LIMIT), while these three are
    the natural-language forms a REDUCER-filed successor would use, and the
    reducer is exactly the filer the case-letter marker cannot reach.

    An unpinned token in a guard is indistinguishable from dead weight the next
    reader should delete. This test is what makes that judgement explicit rather
    than leaving it to a mutation run nobody will repeat.
    """
    mod = _import_sweep()
    for token, sentence in (
        ("successor",
         "This is the successor of g-350-202, carrying what that goal did not "
         "finish."),
        ("unfinished remainder",
         "Filed while closing g-350-202 — this is the unfinished remainder of "
         "that goal."),
        ("sanctioned scope",
         "Preserves the sanctioned scope of g-350-202; not new agenda."),
    ):
        g = {"id": "g-x", "title": "Unblock: x", "description": sentence}
        assert mod._successor_marker_guard(g) is not None, token
        # ...and the sentence carries NO case letter, so the pin fails if the
        # token is deleted rather than passing via a different branch.
        assert "case A" not in sentence and "Case A" not in sentence


def test_g115_6252_missing_description_does_not_crash():
    """A goal with no description must return None, not raise — the sweep runs
    unattended from aspirations-precheck --apply."""
    mod = _import_sweep()
    assert mod._successor_marker_guard({"id": "g-x", "title": "Unblock: x"}) is None
    assert mod._successor_marker_guard(
        {"id": "g-x", "title": "Unblock: x", "description": None}) is None


# ── the marker must NOT fire on ordinary English (, 2026-08-15) ─────
# The suite above pins that each marker word FIRES. Nothing pinned the other
# half, and that is exactly where the defect lived: a bare `[Rr]emainder` branch
# matched ordinary prose in 3 of 3 live instances hand-checked and ZERO genuine
# successors. A hit here makes an ORDINARY Unblock permanently undischargeable —
# the  under-discharge direction, introduced by the fix for the
# opposite one, exactly as 's description predicted ("a fix that only
# narrows it makes  worse").

def test_successor_guard_does_not_fire_on_ordinary_english_remainder():
    mod = _import_sweep()
    # All three verbatim from live goal descriptions on 2026-08-15.
    for gid, prose in (
        ("g-115-4212", "it will read fresh for the entire remainder of the day "
                       "no matter how many adds land"),
        ("g-115-4216", "g-115-4166 closed the literal asp-115 write sites (25 -> 1, "
                       "the remainder being cross-world-inject-goal.sh:109)"),
    ):
        g = {"title": "Unblock: x for g-1-1", "description": prose, "origin_signal": ""}
        assert mod._successor_scope_guard(g) is None, (
            f"{gid}: ordinary-English 'remainder' must not read as a successor marker")


def test_successor_remainder_still_fires_when_it_names_its_object():
    """The narrowing kept the author's intent from 's own fixture — the
    successor sense of 'remainder' is always OF something. Requiring the object is
    the entire difference from the retired bare token."""
    mod = _import_sweep()
    for prose in ("carries the remainder of the parent's outcome",
                  "picks up the remainder of g-350-202's sanctioned scope",
                  "the unfinished remainder of that work"):
        g = {"title": "Unblock: x for g-1-1", "description": prose, "origin_signal": ""}
        assert mod._successor_scope_guard(g) is not None, prose


def test_the_two_hand_verified_successors_still_match_on_case_letter():
    """Both real successors on the live corpus match `[Cc]ase A`, not `remainder`.
    That is WHY retiring the bare token cost no coverage — pinned so a future
    'tidy' of the case-letter branch cannot quietly remove the only token that
    actually protects them."""
    mod = _import_sweep()
    g = {"title": "Unblock: provision the DEV-place host infra",
         "description": "FILED BY alpha WORKER BODY ON cc-08 (case A - successor "
                        "preserving g-350-202 sanctioned scope: ...)",
         "origin_signal": ""}
    reason = mod._successor_scope_guard(g)
    assert reason is not None and "case A" in reason


def test_bare_remainder_branch_stays_retired():
    """Mutation guard. The retired branch is invisible to the behavioural tests
    above once it is gone, so pin the pattern itself — re-adding `|\\b[Rr]emainder\\b`
    would make all three assertions above pass ONLY because the narrowed branch
    also matches, and the regression would ship green."""
    mod = _import_sweep()
    assert r"\b[Rr]emainder\b" not in mod.SUCCESSOR_MARKER_PATTERN.pattern
    assert r"[Rr]emainder of (?:the parent|g-\d)" in mod.SUCCESSOR_MARKER_PATTERN.pattern
