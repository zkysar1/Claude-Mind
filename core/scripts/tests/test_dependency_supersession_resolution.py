#!/usr/bin/env python3
"""Pin supersession-aware dependency resolution ().

THE SCENARIO THESE TESTS EXIST FOR (measured 2026-08-26, ~4h fleet blindness):
bravo closed a duplicate cutover chain (g-364-77..80) as `skipped`, recording
the supersession only in each goal's outcome_note. echo's re-probe sweep read
`status == "skipped"` as NOT-done and re-deferred two goals bravo had just
unblocked, citing a premise ("cutover not live") that was already false. Zero
vinheim goals were selectable across 1,400 ranked until a human asked.

A status-equality check cannot see that the work IS done under a different id.
`test_echo_refreeze_scenario_does_not_refreeze` is the regression pin.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from _dependency_graph import (  # noqa: E402
    SUPERSEDABLE_STATUSES,
    resolve_dependency,
    supersession_target,
)


# --- the regression pin ----------------------------------------------------


def test_echo_refreeze_scenario_does_not_refreeze():
    """The literal 2026-08-26 shape: skipped + supersession note -> completed."""
    idx = {
        "g-364-79": {
            "status": "skipped",
            "outcome_note": "Duplicate cutover chain. Superseded by g-364-81, "
                            "which carries the live cutover.",
        },
        "g-364-81": {"status": "completed"},
    }
    verdict, resolved, _chain = resolve_dependency("g-364-79", idx)
    assert verdict == "satisfied", (
        "a defer citing a superseded goal MUST NOT re-freeze when the "
        f"superseding goal is completed — got {verdict!r}")
    assert resolved == "g-364-81"


def test_explicit_pointer_beats_the_note_fallback():
    """superseded_by is authoritative; the note is only the migration path."""
    idx = {
        "a": {"status": "superseded", "superseded_by": "real",
              "outcome_note": "superseded by decoy"},
        "real": {"status": "completed"},
        "decoy": {"status": "pending"},
    }
    assert resolve_dependency("a", idx)[:2] == ("satisfied", "real")


def test_chain_of_duplicates_resolves_to_whoever_did_the_work():
    idx = {
        "a": {"status": "superseded", "superseded_by": "b"},
        "b": {"status": "superseded", "superseded_by": "c"},
        "c": {"status": "completed"},
    }
    verdict, resolved, chain = resolve_dependency("a", idx)
    assert (verdict, resolved) == ("satisfied", "c")
    assert chain == ["a", "b", "c"]


# --- the half that must NOT over-satisfy -----------------------------------


def test_supersession_moves_the_obligation_it_does_not_discharge_it():
    """A superseding goal that is itself skipped does NOT satisfy."""
    idx = {
        "a": {"status": "superseded", "superseded_by": "b"},
        "b": {"status": "skipped", "outcome_note": "abandoned, nothing here"},
    }
    assert resolve_dependency("a", idx)[0] == "open"


def test_only_completed_satisfies():
    for status in ("pending", "in-progress", "blocked", "skipped", "expired",
                   "decomposed"):
        idx = {"a": {"status": status}}
        assert resolve_dependency("a", idx)[0] == "open", status
    assert resolve_dependency("a", {"a": {"status": "completed"}})[0] == "satisfied"


def test_absent_target_is_unknown_never_open():
    """guard-1890: an archived completion is absent too — never call it open."""
    assert resolve_dependency("nope", {})[0] == "unknown"


def test_pointer_cycle_is_reported_not_looped_forever():
    idx = {
        "a": {"status": "superseded", "superseded_by": "b"},
        "b": {"status": "superseded", "superseded_by": "a"},
    }
    assert resolve_dependency("a", idx)[0] == "cycle"


def test_self_pointer_is_a_cycle():
    idx = {"a": {"status": "superseded", "superseded_by": "a"}}
    assert resolve_dependency("a", idx)[0] == "cycle"


# --- the note fallback is deliberately narrow ------------------------------


def test_note_fallback_requires_both_a_marker_and_an_id():
    # marker, no id -> no guess
    assert supersession_target(
        {"status": "skipped", "outcome_note": "this supersedes things"}) is None
    # id, no marker -> a mention is not a supersession
    assert supersession_target(
        {"status": "skipped", "outcome_note": "see also g-1-2 for context"}) is None
    # both -> resolves
    assert supersession_target(
        {"status": "skipped", "outcome_note": "superseded by g-1-2"}) == "g-1-2"


def test_note_fallback_only_applies_to_supersedable_statuses():
    """A live goal whose note happens to say 'superseded' is still live."""
    assert supersession_target(
        {"status": "pending", "outcome_note": "superseded by g-1-2"}) is None
    for status in SUPERSEDABLE_STATUSES:
        assert supersession_target(
            {"status": status, "outcome_note": "superseded by g-1-2"}) == "g-1-2"


def test_explicit_pointer_is_honored_regardless_of_status():
    """The field is authoritative — it is not gated on the status guess."""
    assert supersession_target(
        {"status": "pending", "superseded_by": "g-1-2"}) == "g-1-2"


def test_four_digit_goal_ids_parse():
    """asp-115 passed  on 2026-05-19; the id regex must not clip."""
    assert supersession_target(
        {"status": "skipped",
         "outcome_note": "superseded by g-115-7893"}) == "g-115-7893"


def test_non_dict_and_blank_inputs_do_not_raise():
    assert supersession_target(None) is None
    assert supersession_target("nope") is None
    assert supersession_target({"status": "skipped", "superseded_by": "   "}) is None
    assert supersession_target({"status": "skipped"}) is None


# --- the WIRING (guard-3585: verify the EFFECT, in the sibling module) ------
#
# The live sweep returns 0 today because the store holds 0 superseded goals, so
# a green live run proves NOTHING about this branch. These exercise the real
# classifier in blocked-signal-resolution-check.py (precheck 0.5b.12).

import importlib.util  # noqa: E402


def _classifier():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "blocked-signal-resolution-check.py")
    spec = importlib.util.spec_from_file_location("_bsrc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _classify(mod, rid, goal_index):
    """goal_index in the sweep's own shape: {gid: (source, goal_dict)}."""
    return mod._classify_ref(rid, goal_index, {}, True)[:1][0]


def test_sweep_resolves_a_superseded_dependency_as_satisfied():
    mod = _classifier()
    idx = {
        "g-364-79": ("world", {"status": "skipped",
                               "outcome_note": "dup; superseded by g-364-81"}),
        "g-364-81": ("world", {"status": "completed"}),
    }
    assert _classify(mod, "g-364-79", idx) is True


def test_sweep_does_not_over_satisfy_when_the_superseder_is_not_done():
    mod = _classifier()
    idx = {
        "a": ("world", {"status": "superseded", "superseded_by": "b"}),
        "b": ("world", {"status": "pending"}),
    }
    assert _classify(mod, "a", idx) is False


def test_sweep_returns_undecidable_on_a_cyclic_chain():
    mod = _classifier()
    idx = {
        "a": ("world", {"status": "superseded", "superseded_by": "b"}),
        "b": ("world", {"status": "superseded", "superseded_by": "a"}),
    }
    assert _classify(mod, "a", idx) is None


def test_sweep_plain_statuses_are_unchanged_by_the_new_branch():
    """Regression: the common path must behave exactly as before."""
    mod = _classifier()
    assert _classify(mod, "a", {"a": ("world", {"status": "completed"})}) is True
    assert _classify(mod, "a", {"a": ("world", {"status": "pending"})}) is False
    # skipped with NO supersession pointer keeps its pre-existing reading
    assert _classify(mod, "a", {"a": ("world", {"status": "skipped"})}) is True
