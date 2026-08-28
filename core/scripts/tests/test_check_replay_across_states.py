#!/usr/bin/env python3
"""Fixtures for the across-state check replay classifier (gap-089 forge).

WHAT THIS SEAM EXCLUDES, said plainly because a fixture injection point is a
silent scope declaration (guard-1462): every test below drives `classify()`,
which is PURE. So worktree creation, the local-paths.conf copy, the
STORAGE_BACKEND pin, the MIND_WORLD/META pops and the teardown call are all
UPSTREAM of this seam and are structurally unfalsifiable here. They were
validated by a LIVE two-state run against this repo (recorded in the goal's
outcome_note), which is the only thing that can reach them.

The anti-vacuity guard is `test_the_six_shapes_do_not_collapse`. Per guard-1793
it is mutated against ON ITS OWN, not via the suite: an aggregate that stays
green through a defect it was written to catch is not a health check.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from check_replay_across_states import classify  # noqa: E402


def S(short, passed):
    return {"short": short, "passed": passed}


# ── the four documented outcomes, each a DISTINCT verdict ────────────────────

def test_green_then_red_is_a_regression():
    v, why = classify([S("aaa", True), S("bbb", False)])
    assert v == "REGRESSION"
    assert "introduced" in why


def test_red_then_green_is_fixed_not_a_regression():
    """The  case: the reading that INVERTED under replay.

    Reporting this span as a regression would revert work that fixed nine
    pre-existing reds — the most expensive possible direction to be wrong in.
    """
    v, why = classify([S("aaa", False), S("bbb", True)])
    assert v == "FIXED"
    assert "do NOT report as a regression" in why


def test_red_at_both_ends_is_pre_existing():
    v, why = classify([S("aaa", False), S("bbb", False)])
    assert v == "PRE_EXISTING"
    assert "owning goal" in why


def test_green_at_both_ends_does_not_claim_all_clear():
    """STILL_GREEN must not be readable as 'nothing is wrong'.

    The solo-vs-suite (environmental) axis is invisible to this module, so the
    reason string has to say so or the verdict over-claims.
    """
    v, why = classify([S("aaa", True), S("bbb", True)])
    assert v == "STILL_GREEN"
    assert "solo-vs-suite axis was not examined" in why


# ── edge shapes ──────────────────────────────────────────────────────────────

def test_multiple_transitions_refuse_a_single_endpoint_verdict():
    """green,red,green endpoints are green->green, but calling that STILL_GREEN
    would hide a real break in the middle. More than one flip must escalate."""
    v, why = classify([S("a", True), S("b", False), S("c", True)])
    assert v == "MIXED"
    assert "flips more than once" in why


def test_monotone_span_across_many_states_keeps_the_endpoint_verdict():
    # exactly one transition over 4 states is still a clean REGRESSION
    v, _ = classify([S("a", True), S("b", True), S("c", False), S("d", False)])
    assert v == "REGRESSION"


def test_invalid_states_are_skipped_not_counted_as_pass():
    """An unevaluable state (no conf, worktree failure, timeout) must never be
    silently folded in as a pass — that is how an INVALID run reads as green."""
    v, _ = classify([S("a", None), S("b", False), S("c", False)])
    assert v == "PRE_EXISTING"


def test_fewer_than_two_evaluated_states_is_indeterminate():
    v, why = classify([S("a", True), S("b", None)])
    assert v == "INDETERMINATE"
    assert "need >= 2" in why


def test_all_states_invalid_is_indeterminate_not_still_green():
    v, _ = classify([S("a", None), S("b", None)])
    assert v == "INDETERMINATE"


# ── anti-vacuity (guard-1220 two-way proof; mutate THIS one per guard-1793) ──

def test_the_six_shapes_do_not_collapse():
    """Six inputs, six DISTINCT verdicts.

    A classifier that answered the same way for every shape would pass each
    test above only if that test's own string happened to match — this is the
    assertion that fails if the mapping is ever flattened.
    """
    shapes = {
        "REGRESSION":    [S("a", True),  S("b", False)],
        "FIXED":         [S("a", False), S("b", True)],
        "PRE_EXISTING":  [S("a", False), S("b", False)],
        "STILL_GREEN":   [S("a", True),  S("b", True)],
        "MIXED":         [S("a", True),  S("b", False), S("c", True)],
        "INDETERMINATE": [S("a", True),  S("b", None)],
    }
    got = {name: classify(v)[0] for name, v in shapes.items()}
    assert got == {k: k for k in shapes}, got
    assert len(set(got.values())) == 6
