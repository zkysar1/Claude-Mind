#!/usr/bin/env python3
"""test_intent_satisfaction_evidence_arithmetic.py —  regression test.

Pins the evidence-cardinality arithmetic in `_validate_intent_satisfaction`
against the defect that made the aspiration-close gate MATHEMATICALLY
UNSATISFIABLE.

── The bug ──────────────────────────────────────────────────────────────────
The THRESHOLD was computed against the full non-recurring goal count:

    required = max(scope_min, ceil(0.5 * len(non_recurring)))

while the QUALIFYING POOL — the goals the very next loop in the same function
will accept — is restricted to completed, non-recurring goals carrying
`verification.outcomes`. Whenever outcome coverage fell below 50%, no evidence
set could satisfy both halves, and the caller bounced between the two refusals
forever. Measured verbatim on ZDS asp-008 (2026-07-30/31):

    "evidence_goal_ids has 7, scope=project requires >=37
     (max of 3-by-scope and ceil(0.5 * 73 non-recurring))"
    "evidence goal g-008-17 has no verification.outcomes"

    non-recurring goals ......................... 73
    threshold ................................... 37
    completed goals WITH verification.outcomes .. 30   <- honest qualifying pool

30 < 37, so the aspiration could never be closed on honest evidence no matter
what was supplied. The close was attempted for real and refused TWICE.

── The fix, and why min() rather than a relaxation ──────────────────────────
    required = max(scope_min, min(ceil(0.5 * len(non_recurring)), len(qualifying)))

min() is strictly STRONGER than relaxing to half the qualifying pool: it demands
EVERY available piece of honest evidence (30 of 30 on asp-008). Where coverage is
healthy (qualifying >= ceil) it is a no-op — pinned by
`test_healthy_coverage_threshold_unchanged`, so the anti-thin-evidence intent is
preserved rather than traded away.

`scope_min` deliberately remains a HARD floor. An aspiration with fewer
qualifying goals than the floor has genuinely thin evidence and should not be
intent-closed at all — but it now says so outright and names the alternative
destinations, instead of letting the caller discover it by bouncing off the
quality loop one id at a time. That honest-refusal path is pinned by
`test_below_scope_floor_refuses_with_actionable_message`.

── Why this file parametrizes over TWO implementations ──────────────────────
The validator is MIRRORED: `core/scripts/aspirations.py` (CLI) and
`mind_api/src/endpoints/aspirations_write.py` (daemon). Under daemon-only
architecture the daemon copy is the LIVE path, and the incident's quoted
refusal matched the daemon's wording (it lacks the CLI's trailing
"; cannot serve as intent evidence"), so that is the copy that actually
produced the measured failures.

Fixing one copy and testing one copy is the exact generalization-remainder
class encoded as guard-2078 — a fix scoped to one instance of a shared
mechanism leaves the sibling broken while reading as complete. Every test below
therefore runs against BOTH copies; a drift in either one fails.
"""

from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "core" / "scripts"))
sys.path.insert(0, str(ROOT))

_cli = importlib.import_module("aspirations")
_daemon = importlib.import_module("mind_api.src.endpoints.aspirations_write")

# Both copies of the mirrored validator. Parametrizing here (rather than testing
# the CLI alone) is the point of the file — see the module docstring.
IMPLS = [
    pytest.param(_cli._validate_intent_satisfaction, id="cli"),
    pytest.param(_daemon._validate_intent_satisfaction, id="daemon"),
]

CONFIG = {"min_evidence_by_scope": {"sprint": 2, "project": 3, "initiative": 5}}

MOTIVATION = "deliver the reporting pipeline end to end"
RATIONALE = ("The reporting pipeline was delivered end to end; every remaining "
             "goal is superseded or already terminal.")


def _goal(gid, *, outcomes=False, status="completed", recurring=False):
    g = {"id": gid, "status": status, "recurring": recurring}
    if outcomes:
        g["verification"] = {"outcomes": ["did the thing"]}
    return g


def _asp(n_total, n_with_outcomes, *, scope="project", asp_id="asp-t"):
    """n_total non-recurring goals, of which n_with_outcomes carry outcomes.

    Every goal is `completed`, so rule 6 (all non-recurring terminal after
    supersession) is satisfied with an empty superseded list — this isolates the
    cardinality arithmetic, which is what the goal under test is about. The shape
    mirrors asp-008: 67 completed, most WITHOUT outcomes.
    """
    goals = [_goal(f"g-{i}", outcomes=(i < n_with_outcomes)) for i in range(n_total)]
    return {"id": asp_id, "scope": scope, "motivation": MOTIVATION, "goals": goals}


def _block(ev_ids):
    return {"evidence_goal_ids": list(ev_ids), "rationale": RATIONALE,
            "superseded_goal_ids": []}


def _qualifying_ids(asp):
    return [g["id"] for g in asp["goals"]
            if not g.get("recurring")
            and g.get("status") == "completed"
            and (g.get("verification") or {}).get("outcomes")]


@pytest.mark.parametrize("validate", IMPLS)
def test_sub_50pct_outcome_coverage_is_satisfiable(validate):
    """THE required outcome: N non-recurring goals, fewer than N/2 carrying
    verification.outcomes, and the close gate is SATISFIABLE on honest evidence.

    10 non-recurring / 4 with outcomes. 4 < 5 = N/2, so the OLD formula demanded
    5 from a pool that could only ever supply 4 — unsatisfiable. The assertion
    below on `old_required` is not decoration: it proves this fixture actually
    reproduces the defect, so a regression cannot pass this test vacuously.
    """
    asp = _asp(10, 4)
    qualifying = _qualifying_ids(asp)
    assert len(qualifying) == 4

    old_required = max(3, math.ceil(0.5 * 10))
    assert old_required > len(qualifying), "fixture must reproduce the unsatisfiable shape"

    ok, err = validate(asp, _block(qualifying), CONFIG)
    assert ok, f"gate must be satisfiable on the full honest pool; refused with: {err}"
    assert err is None


@pytest.mark.parametrize("validate", IMPLS)
def test_full_honest_pool_is_demanded_not_half_of_it(validate):
    """min() is STRONGER than relaxing to half the qualifying pool: with coverage
    below 50%, every qualifying goal is required. One short still refuses."""
    asp = _asp(10, 4)
    qualifying = _qualifying_ids(asp)

    ok, err = validate(asp, _block(qualifying[:-1]), CONFIG)
    assert not ok
    assert "requires >=4" in err


@pytest.mark.parametrize("validate", IMPLS)
def test_healthy_coverage_threshold_unchanged(validate):
    """Scope fence: where the qualifying pool already exceeds ceil(0.5 * N), the
    cap is a no-op and the original threshold still governs. This is what keeps
    the fix from being a blanket relaxation of the evidence bar."""
    asp = _asp(10, 8)
    qualifying = _qualifying_ids(asp)
    assert len(qualifying) == 8

    # min(ceil(0.5 * 10), 8) == 5 — identical to the pre-fix threshold.
    ok, err = validate(asp, _block(qualifying[:4]), CONFIG)
    assert not ok, "4 evidence goals must still be refused on a healthy aspiration"
    assert "requires >=5" in err

    ok, err = validate(asp, _block(qualifying[:5]), CONFIG)
    assert ok, f"5 evidence goals must satisfy the unchanged threshold; got: {err}"


@pytest.mark.parametrize("validate", IMPLS)
def test_below_scope_floor_refuses_with_actionable_message(validate):
    """scope_min stays a HARD floor — genuinely thin evidence is still refused.
    But the refusal must NAME itself as unsatisfiable and point at the paths that
    can discharge it, rather than sending the caller back to supply more ids that
    the quality loop will reject one at a time (the dead end being fixed)."""
    asp = _asp(10, 2)                      # 2 qualifying < scope_min 3
    ok, err = validate(asp, _block(_qualifying_ids(asp)), CONFIG)
    assert not ok
    assert "cannot be intent-closed" in err
    assert "retire" in err
    assert "2 of 10" in err


@pytest.mark.parametrize("validate", IMPLS)
def test_scope_floor_still_applies_to_small_aspirations(validate):
    """A tiny aspiration cannot slip under the floor via the new cap: 4 goals all
    carrying outcomes yields min(2, 4) == 2, but scope_min 3 still governs."""
    asp = _asp(4, 4)
    qualifying = _qualifying_ids(asp)
    ok, err = validate(asp, _block(qualifying[:2]), CONFIG)
    assert not ok
    assert "requires >=3" in err

    ok, err = validate(asp, _block(qualifying[:3]), CONFIG)
    assert ok, f"3 evidence goals must clear the project floor; got: {err}"


@pytest.mark.parametrize("validate", IMPLS)
def test_asp_008_measured_shape(validate):
    """The live incident, at its measured numbers: 73 non-recurring, 30 completed
    with outcomes, scope=project. Pre-fix required 37 from a pool of 30. Post-fix
    the threshold is exactly 30 — the whole honest pool, nothing relaxed."""
    asp = _asp(73, 30, asp_id="asp-008")
    qualifying = _qualifying_ids(asp)
    assert len(qualifying) == 30
    assert max(3, math.ceil(0.5 * 73)) == 37                 # the refusal omni measured

    ok, err = validate(asp, _block(qualifying[:29]), CONFIG)
    assert not ok and "requires >=30" in err                 # not one goal cheaper

    ok, err = validate(asp, _block(qualifying), CONFIG)
    assert ok, f"asp-008's honest evidence must now close it; refused with: {err}"


@pytest.mark.parametrize("validate", IMPLS)
def test_quality_loop_still_rejects_outcome_less_evidence(validate):
    """Scope fence: the cap changes only the COUNT required, never what counts.
    A goal without verification.outcomes is still not evidence — otherwise the
    fix would have quietly widened the pool instead of correcting the arithmetic."""
    asp = _asp(10, 4)
    padded = _qualifying_ids(asp) + ["g-9"]                  # g-9 carries no outcomes
    ok, err = validate(asp, _block(padded), CONFIG)
    assert not ok
    assert "g-9" in err and "verification.outcomes" in err


# ── : the two halves of the gate must AGREE ────────────────────────
#
# The threshold is capped by a qualifying POOL; the quality loop ~15 lines below
# re-tests the SAME membership conditions in separate code so it can name WHICH
# goal failed and WHY. That is the  defect reproduced in miniature: add
# a 4th condition to the loop and the pool over-counts, so the gate silently
# becomes unsatisfiable again for records failing only the new condition.
#
# The coupling is a TEST rather than shared control flow, deliberately. Collapsing
# the loop into one boolean would trade real diagnostic quality for a cosmetic
# dedup, and rb-6012's rule is that the halves must AGREE, not that they must
# share code. The pool half is reached through the REAL predicate imported from
# each module -- adding a fourth private copy of the conditions here would be the
# very drift under test (the older helper `_qualifying_ids` above is a third copy,
# scoped to the arithmetic fixtures and deliberately left alone).

MODULES = [pytest.param(_cli, id="cli"), pytest.param(_daemon, id="daemon")]

# scope floor of 1, so a ONE-goal probe aspiration clears the cardinality check and
# actually reaches the quality loop. With a higher floor every probe would bounce
# off "requires >=N" and the test would measure a branch it is not about.
SOLO_CONFIG = {"min_evidence_by_scope": {"solo": 1}}

# The four refusals the quality loop can emit, in wording common to BOTH copies
# (the CLI appends "; cannot serve as intent evidence" to the last one).
LOOP_REASONS = ("not in aspiration", "is recurring", "must be completed",
                "no verification.outcomes")

# One goal per membership-condition combination. Every non-recurring entry is
# reached by both halves; the recurring ones are excluded by the pool's own
# iteration and by the loop's explicit branch.
CORPUS = [
    ("completed+outcomes",           "completed", True,  False),
    ("completed+no-outcomes",        "completed", False, False),
    ("completed+empty-outcomes",     "completed", None,  False),
    ("pending+outcomes",             "pending",   True,  False),
    ("pending+no-outcomes",          "pending",   False, False),
    ("blocked+outcomes",             "blocked",   True,  False),
    ("skipped+outcomes",             "skipped",   True,  False),
    ("recurring+completed+outcomes", "completed", True,  True),
    ("recurring+pending+no-outcomes","pending",   False, True),
]


def _corpus_goal(gid, status, outcomes, recurring):
    g = {"id": gid, "status": status, "recurring": recurring}
    if outcomes is True:
        g["verification"] = {"outcomes": ["did the thing"]}
    elif outcomes is None:                      # present but EMPTY -- falsy, must not qualify
        g["verification"] = {"outcomes": []}
    return g


def _loop_verdict(validate, goal):
    """Does the QUALITY LOOP accept `goal` as evidence? -> (accepted, err).

    Drives the real validator with `goal` as the sole evidence of a one-goal
    aspiration. Any refusal is asserted to be a quality-loop refusal that NAMES
    the goal, so this probe cannot silently measure the cardinality branch or a
    later check instead -- and that assertion is also what pins the
    no-diagnostic-regression outcome.
    """
    gid = goal["id"]
    asp = {"id": "asp-agree", "scope": "solo", "motivation": MOTIVATION,
           "goals": [goal]}
    block = {"evidence_goal_ids": [gid], "rationale": RATIONALE,
             "superseded_goal_ids": []}
    ok, err = validate(asp, block, SOLO_CONFIG)
    if ok:
        return True, None
    assert gid in err, f"loop refusal must name the goal; got: {err}"
    assert any(r in err for r in LOOP_REASONS), (
        f"refusal for {gid} did not come from the quality loop -- the probe "
        f"measured a different branch: {err}")
    return False, err


@pytest.mark.parametrize("mod", MODULES)
def test_qualifying_pool_agrees_with_quality_loop(mod):
    """Per-goal agreement between the pool predicate and the quality loop.

    If either half gains a membership condition the other lacks, some corpus goal
    lands in exactly one of them and this fails -- which is the whole point, since
    that divergence is silent in production until an aspiration cannot be closed.
    """
    qualifies = mod._qualifies_as_intent_evidence
    validate = mod._validate_intent_satisfaction

    accepted, reasons = [], []
    for label, status, outcomes, recurring in CORPUS:
        goal = _corpus_goal(f"g-{label}", status, outcomes, recurring)
        pool_says = bool(qualifies(goal))
        loop_says, err = _loop_verdict(validate, goal)
        assert pool_says == loop_says, (
            f"{label}: qualifying pool says {pool_says} but the quality loop says "
            f"{loop_says} -- the two halves of the gate have drifted apart. "
            f"Loop said: {err}")
        accepted.append(loop_says)
        if err:
            reasons.append(next(r for r in LOOP_REASONS if r in err))

    # Anti-vacuity: a corpus that is all-accept or all-reject would agree with a
    # predicate stuck at a constant, so the assertions above would prove nothing.
    assert any(accepted), "corpus must contain at least one ACCEPTED shape"
    assert not all(accepted), "corpus must contain at least one REJECTED shape"

    # No diagnostic regression: the loop still discriminates BETWEEN conditions
    # rather than emitting one generic refusal. Checked in addition to -- never
    # instead of -- the per-goal assertions above, which are what actually fail
    # when the halves drift (an aggregate can stay green through a defect that
    # moves a different axis).
    assert len(set(reasons)) >= 3, (
        f"quality loop must keep a distinct message per condition; saw only "
        f"{sorted(set(reasons))}")


@pytest.mark.parametrize("mod", MODULES)
def test_evidence_goal_outside_the_aspiration_is_rejected_by_the_loop_alone(mod):
    """The loop's in-aspiration check has NO pool counterpart, by construction.

    The pool is built by iterating the aspiration's own goals, so a foreign id can
    never be in it -- the halves agree trivially. Pinned separately because it is
    the one loop branch the predicate cannot express (it is a property of the
    aspiration, not of a goal), and a reader who found it missing from the corpus
    above would otherwise have to re-derive why.
    """
    validate = mod._validate_intent_satisfaction
    resident = _corpus_goal("g-resident", "completed", True, False)
    asp = {"id": "asp-agree", "scope": "solo", "motivation": MOTIVATION,
           "goals": [resident]}
    block = {"evidence_goal_ids": ["g-ghost"], "rationale": RATIONALE,
             "superseded_goal_ids": []}
    ok, err = validate(asp, block, SOLO_CONFIG)
    assert not ok
    assert "g-ghost" in err and "not in aspiration" in err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
