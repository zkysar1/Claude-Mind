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


# --- SENTENCE SCOPE: the fallback reads one sentence, not the whole note -----
#
# Added 2026-08-27 when the wiring below moved this resolver from a diagnostic
# path onto the SELECTION path, which is what made its precision load-bearing.
# Measured over the 32 live pointer-carrying rows: "first goal-id anywhere in
# the note" was wrong three different ways, and each way has a test here.
#
# guard-4166 governs the shape of this section: every one of these fixes makes
# something STOP resolving, so each drop assertion is PAIRED with a keep
# assertion IN THE SAME CALL. An over-broad narrowing that returned None for
# everything would pass the drops and fail the keeps.


def test_self_header_does_not_steal_the_target():
    """The live `` shape.

    The closure-note convention opens with a markdown header naming the goal
    ITSELF, so first-id resolved the goal to itself and `resolve_dependency`
    answered `cycle` — a false data-defect verdict while the real successor sat
    one clause away. The self-mention must be skipped, and the NEXT id kept.
    """
    goal = {
        "id": "g-001-374",
        "status": "skipped",
        "outcome_note": (
            "# g-001-374 — SKIPPED: duplicate of g-001-372\n"
            "Closing this one as superseded; the surviving record is "
            "g-001-372, now enriched with everything this unit measured."),
    }
    assert supersession_target(goal) == "g-001-372", supersession_target(goal)


def test_marker_whose_object_is_not_a_goal_resolves_to_nothing():
    """`SUPERSEDES the prior-occurrence note` — real marker, non-goal object.

    Three live rows supersede a NOTE, a CONVENTION and a PREMISE. The id that
    first-id found was unrelated to any of them. Paired positive control: the
    same prose with an id IN the marker's sentence still resolves, so this is a
    scope narrowing and not a blanket refusal.
    """
    note = ("[worker-loop] SUPERSEDES the prior-occurrence note of 4556 "
            "chars.\nPrior text recoverable from git history under g-364-54.")
    assert supersession_target({"id": "g-364-59", "status": "skipped",
                                "outcome_note": note}) is None
    keep = ("[worker-loop] SUPERSEDES the prior occurrence via g-364-54.\n"
            "Prior text recoverable from git history.")
    assert supersession_target({"id": "g-364-59", "status": "skipped",
                                "outcome_note": keep}) == "g-364-54"


def test_an_id_before_the_marker_is_not_the_target():
    """The live `` shape: the note names the chain it belonged to
    BEFORE saying who superseded it. First-id returned the chain member; the
    successor is the id that FOLLOWS the marker."""
    goal = {
        "id": "g-364-80",
        "status": "skipped",
        "outcome_note": ("SSM hash-confirm, whose chain (g-364-77/78/79) was "
                         "superseded by g-364-11's direct execution."),
    }
    assert supersession_target(goal) == "g-364-11", supersession_target(goal)


def test_superseded_by_time_names_no_successor():
    """Six live rows read 'superseded by weeks of later runs on the same
    workflow.' — superseded by TIME. There is no successor goal, so there is
    no pointer; the id later in the note belongs to a different thought.

    Paired keep: the identical opening with a successor named IN the sentence.
    """
    drop = ("Stale CI-failure alert — superseded by weeks of later runs on "
            "the same workflow. The 2026-08-24 rerun sweep (g-115-7683) "
            "covers it.")
    assert supersession_target({"id": "g-115-7536", "status": "skipped",
                                "outcome_note": drop}) is None
    keep = ("Stale CI-failure alert — superseded by g-115-7683, the "
            "2026-08-24 account-wide rerun sweep.")
    assert supersession_target({"id": "g-115-7536", "status": "skipped",
                                "outcome_note": keep}) == "g-115-7683"


def test_sentence_ends_at_a_period_or_a_newline():
    """Both terminators, because these notes are markdown: a heading or bullet
    ends the thought without a period. Each drop is paired with the same text
    minus the boundary, which must resolve."""
    for boundary in (". ", ".\n", "\n"):
        drop = "superseded" + boundary + "The successor is g-1-2."
        assert supersession_target({"id": "g-1-1", "status": "skipped",
                                    "outcome_note": drop}) is None, repr(boundary)
        keep = "superseded, successor is g-1-2" + boundary + "Done."
        assert supersession_target({"id": "g-1-1", "status": "skipped",
                                    "outcome_note": keep}) == "g-1-2", repr(boundary)


def test_a_period_inside_a_token_does_not_end_the_sentence():
    """The boundary is `. ` / `.<newline>` / end-of-note, NOT a bare dot.

    Deliberate: these notes are dense with filenames, versions and shas
    (`aspirations.jsonl`, `v1.2`), and breaking on every dot would truncate the
    marker's sentence before its successor and silently drop a real pointer.
    Discovered by this test's own first draft, whose fixture used a bare dot
    and asserted the drop — the code was right and the fixture was not.
    """
    note = "superseded in aspirations.jsonl v1.2 by g-1-2."
    assert supersession_target({"id": "g-1-1", "status": "skipped",
                                "outcome_note": note}) == "g-1-2"


def test_explicit_field_is_untouched_by_sentence_scope():
    """The narrowing applies to the PROSE fallback only. A machine-written
    pointer is authoritative and never re-derived — including a self-pointer,
    which stays a reportable `cycle` rather than being silently dropped."""
    assert supersession_target(
        {"id": "g-1-1", "status": "skipped", "superseded_by": "g-9-9",
         "outcome_note": "no marker and no id here"}) == "g-9-9"
    idx = {"g-1-1": {"id": "g-1-1", "status": "superseded",
                     "superseded_by": "g-1-1"}}
    assert resolve_dependency("g-1-1", idx)[0] == "cycle"


def test_later_marker_is_reached_when_the_first_names_nobody():
    """Every marker in the note is tried, not just the first — otherwise a
    leading header sentence would mask a real pointer further down."""
    note = ("# g-1-1 — SKIPPED as superseded, not unfinished.\n"
            "The work was superseded by g-1-2.")
    assert supersession_target({"id": "g-1-1", "status": "skipped",
                                "outcome_note": note}) == "g-1-2"


# --- supersession_satisfied_ids: the additive bridge to a flat done_ids set --


def _sat_ids(*args, **kw):
    from _dependency_graph import supersession_satisfied_ids
    return supersession_satisfied_ids(*args, **kw)


def test_satisfied_ids_adds_only_chains_that_reach_completed():
    """One call, four fixtures, so an implementation that added everything (or
    nothing) fails on the same assertion pair."""
    idx = {
        "g-1-1": {"id": "g-1-1", "status": "skipped",
                  "outcome_note": "superseded by g-1-2."},      # -> completed
        "g-1-2": {"id": "g-1-2", "status": "completed"},
        "g-1-3": {"id": "g-1-3", "status": "skipped",
                  "outcome_note": "superseded by g-1-4."},      # -> still open
        "g-1-4": {"id": "g-1-4", "status": "pending"},
        "g-1-5": {"id": "g-1-5", "status": "skipped",
                  "outcome_note": "superseded by g-9-9."},      # -> unknown
        "g-1-6": {"id": "g-1-6", "status": "skipped",
                  "outcome_note": "closed, no successor named."},
    }
    got = _sat_ids(idx)
    assert got == {"g-1-1"}, got


def test_satisfied_ids_is_strictly_additive():
    """The safety property. Union-in only: the caller's own set is returned
    whole, so no bug here can un-resolve a dependency the caller had already
    satisfied. Paired with a positive control proving the call is not a no-op.
    """
    idx = {
        "g-1-1": {"id": "g-1-1", "status": "skipped",
                  "outcome_note": "superseded by g-1-2."},
        "g-1-2": {"id": "g-1-2", "status": "completed"},
        "g-1-9": {"id": "g-1-9", "status": "completed"},
    }
    done = {"g-1-2", "g-1-9", "g-7-7"}
    expanded = done | _sat_ids(idx, already_done=done)
    assert done <= expanded, "expansion must never REMOVE a done id"
    assert expanded - done == {"g-1-1"}, expanded - done


def test_satisfied_ids_skips_ids_the_caller_already_holds():
    idx = {
        "g-1-1": {"id": "g-1-1", "status": "skipped",
                  "outcome_note": "superseded by g-1-2."},
        "g-1-2": {"id": "g-1-2", "status": "completed"},
    }
    assert _sat_ids(idx) == {"g-1-1"}
    assert _sat_ids(idx, already_done={"g-1-1"}) == set()


def test_satisfied_ids_tolerates_bad_input():
    assert _sat_ids(None) == set()
    assert _sat_ids([]) == set()
    assert _sat_ids({}) == set()
    assert _sat_ids({"g-1-1": None, "g-1-2": "not a dict"}) == set()


# --- the WIRING, part 2: goal-selector's done_ids expansion -----------------
#
# rb-9416: a read-back verifies the WRITE, not the EFFECT. These exercise the
# real function in goal-selector.py against aspiration-shaped fixtures.


def _selector():
    path = os.path.join(os.path.dirname(__file__), "..", "goal-selector.py")
    spec = importlib.util.spec_from_file_location("_gsel", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _asp(goals, status="active"):
    return [{"id": "asp-1", "status": status, "goals": goals}]


def test_selector_expansion_unfreezes_a_superseded_dependency():
    """The echo scenario at the SELECTION surface: the dependency names a goal
    closed `skipped`, so set membership says NOT-done and the dependent stays
    frozen. Following the pointer is what makes the finished work visible."""
    mod = _selector()
    asps = _asp([
        {"id": "g-364-79", "status": "skipped",
         "outcome_note": "Superseded by g-364-11, which carries the cutover."},
        {"id": "g-364-11", "status": "completed"},
        {"id": "g-364-90", "status": "pending", "blocked_by": ["g-364-79"]},
    ])
    done = {"g-364-11"}
    expanded = mod.expand_done_ids_via_supersession(asps, done)
    assert [b for b in ["g-364-79"] if b not in done] == ["g-364-79"], \
        "control: the dependency IS unmet before the expansion"
    assert [b for b in ["g-364-79"] if b not in expanded] == [], \
        "the dependent must be selectable once the pointer is followed"


def test_selector_expansion_changes_nothing_without_a_pointer():
    """guard-2903: an invariance assertion is green by default when the code
    under test never runs. The positive control lives IN THIS TEST — the same
    call shape, one row given a pointer — so a wiring that did nothing at all
    fails the second half."""
    mod = _selector()
    plain = _asp([
        {"id": "g-1-1", "status": "skipped", "outcome_note": "just closed."},
        {"id": "g-1-2", "status": "completed"},
        {"id": "g-1-3", "status": "pending", "blocked_by": ["g-1-1"]},
    ])
    done = {"g-1-2"}
    assert mod.expand_done_ids_via_supersession(plain, done) == done

    pointed = _asp([
        {"id": "g-1-1", "status": "skipped",
         "outcome_note": "superseded by g-1-2."},
        {"id": "g-1-2", "status": "completed"},
        {"id": "g-1-3", "status": "pending", "blocked_by": ["g-1-1"]},
    ])
    assert mod.expand_done_ids_via_supersession(pointed, done) == done | {"g-1-1"}


def test_selector_expansion_ignores_inactive_aspirations():
    """Mirrors the build loops it replaces: they skip non-active aspirations,
    so the index must too, or the expansion resolves against goals the caller
    never counted."""
    mod = _selector()
    done = {"g-1-2"}
    rows = [{"id": "g-1-1", "status": "skipped",
             "outcome_note": "superseded by g-1-2."},
            {"id": "g-1-2", "status": "completed"}]
    assert mod.expand_done_ids_via_supersession(
        _asp(rows, status="completed"), done) == done
    assert mod.expand_done_ids_via_supersession(
        _asp(rows), done) == done | {"g-1-1"}


def test_every_done_ids_build_site_is_expanded():
    """guard-1943: pinning the writer says nothing about the WIRING.

    The three tests above exercise `expand_done_ids_via_supersession` directly,
    so they stay green on a build where the helper exists and NOTHING CALLS IT
    — which is the shape this whole goal exists to prevent (a resolver shipped
    into a module that never consults it). The invariant that actually matters
    is structural: every flat done-ids set goal-selector builds must be
    expanded before it reaches a dependency check, or that code path resolves
    supersession differently from its siblings.

    Counting rather than pattern-matching one known site is deliberate — a
    FOURTH build site added later fails here instead of silently inheriting
    the old semantics.
    """
    path = os.path.join(os.path.dirname(__file__), "..", "goal-selector.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    builds = src.count("global_done_ids = set()") + \
        src.count("global_done_ids_retry = set()")
    calls = src.count("expand_done_ids_via_supersession(") - \
        src.count("def expand_done_ids_via_supersession(")
    assert builds == 3, f"build sites moved: found {builds}, expected 3"
    assert calls == builds, (
        f"{builds} done-ids build sites but {calls} expansion calls — a build "
        "site is resolving supersession differently from its siblings")
