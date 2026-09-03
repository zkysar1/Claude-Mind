""" — the two-loop LIFECYCLE parity contract in worker_execute.py.

WHY THIS FILE EXISTS
The PHASE split has had an SSOT since g-306-69 (`test_worker_execute.py` pins
it). The session LIFECYCLE did not, and every lifecycle asymmetry so far was
found BY SURPRISE, each by a different route: prime never runs for workers
(g-306-211), the per-body heartbeat cannot write on an IDLE worker box
(g-306-208), compact restore rejected body-keyed checkpoints (g-306-174). One
defect class — a reducer lifecycle stage with no DECLARED worker disposition.

WHAT THIS SUITE PINS, AND WHY EACH HALF IS SEPARATE
The module asserts its own contract at import (`_assert_lifecycle_contract`).
That assertion is necessary and NOT sufficient, for the guard-2582 reason: an
in-file check passes vacuously if someone deletes it, and a suite that only
imports the module would go green over exactly that. So this file asserts the
same predicate from OUTSIDE, and — more importantly — proves the predicate has
TEETH by constructing the failures it is supposed to catch and showing they are
refused. A completeness check nobody has ever seen fail is indistinguishable
from `return True`.

That distinction is the lesson from guard-2680, earned on this box two days
ago: a suite that only asserts what a guard STOPS cannot see what the guard
BROKE, and a suite that only asserts the happy path cannot see a guard that
stopped guarding. Both halves are here deliberately.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


we = _load("worker_execute", "worker_execute.py")


# --------------------------- the contract holds ---------------------------

def test_no_lifecycle_gaps():
    """The live table is complete. This is the assertion the module makes about
    itself, restated from outside so deleting the in-file assert is not enough
    to make the tree green (guard-2582)."""
    assert we.lifecycle_gaps() == []


def test_every_canonical_stage_has_a_row():
    missing = [s for s in we.CANONICAL_LIFECYCLE_STAGES
               if s not in we.LIFECYCLE_DISPOSITIONS]
    assert missing == [], f"stages with no disposition row: {missing}"


def test_no_orphan_rows():
    """A rename that updates one side only is the other direction of the same
    defect, and it is the direction a coverage count never notices."""
    canonical = set(we.CANONICAL_LIFECYCLE_STAGES)
    orphans = sorted(set(we.LIFECYCLE_DISPOSITIONS) - canonical)
    assert orphans == [], f"disposition rows for non-canonical stages: {orphans}"


def test_the_table_is_not_vacuously_complete():
    """A floor on stage count — defense-in-depth, NOT the primary guard.

    This test's first docstring claimed `lifecycle_gaps() == []` is satisfied by
    a table with no stages at all. That was written without measuring and is
    FALSE (fresh-eyes F-002): clearing all three tables together yields 10 gaps,
    because every live phase then declares no stage — the phase coupling closes
    the vacuous-pass hole on its own. See
    test_emptying_everything_is_not_a_vacuous_pass, which pins that directly.

    The floor is kept as a cheap second signal that fails loud if the phase
    coupling is ever removed. Pin it well under the current 14 so ordinary growth
    never trips it, and name four stages whose ABSENCE would mean the contract
    was gutted rather than trimmed."""
    assert len(we.CANONICAL_LIFECYCLE_STAGES) >= 10
    for stage in ("select", "claim", "execute", "consolidate-merge"):
        assert stage in we.LIFECYCLE_DISPOSITIONS


def test_emptying_everything_is_not_a_vacuous_pass():
    """The measured claim the docstring above used to assert without measuring.

    Clear all three tables and the contract must STILL report gaps — otherwise a
    gutted table passes as a complete one."""
    sc = we.CANONICAL_LIFECYCLE_STAGES
    sd = dict(we.LIFECYCLE_DISPOSITIONS)
    sp = dict(we.PHASE_LIFECYCLE_STAGE)
    try:
        we.CANONICAL_LIFECYCLE_STAGES = ()
        we.LIFECYCLE_DISPOSITIONS.clear()
        we.PHASE_LIFECYCLE_STAGE.clear()
        gaps = we.lifecycle_gaps()
        assert gaps, (
            "an entirely empty contract reported ZERO gaps — the phase coupling "
            "that makes vacuous-completeness impossible has been removed, and "
            "the stage-count floor is now the only thing standing between this "
            "check and a gutted table")
    finally:
        we.CANONICAL_LIFECYCLE_STAGES = sc
        we.LIFECYCLE_DISPOSITIONS.clear()
        we.LIFECYCLE_DISPOSITIONS.update(sd)
        we.PHASE_LIFECYCLE_STAGE.clear()
        we.PHASE_LIFECYCLE_STAGE.update(sp)
    assert we.lifecycle_gaps() == []


@pytest.mark.parametrize("bad", ["", "  ", 12345, ["g-306-211"], "not-a-goal-id"])
def test_pending_goal_must_be_a_goal_id_or_none(bad):
    """pending_goal was the ONE unvalidated field (fresh-eyes F-001).

    It accepted 12345 and a list while the class docstring claimed every field
    was validated at construction. It is the field whose whole purpose is
    honesty about what has NOT shipped, so junk here reads downstream as a real
    tracker — the worst of the five to leave open."""
    with pytest.raises(ValueError, match="pending_goal must be None or a goal id"):
        we.LifecycleDisposition(
            kind=we.WORKER_ONLY, target="t", why="w", pending_goal=bad)


def test_pending_goal_accepts_none_and_a_real_goal_id():
    """The validator must not be so strict it refuses the live table."""
    assert we.LifecycleDisposition(
        kind=we.WORKER_ONLY, target="t", why="w").pending_goal is None
    assert we.LifecycleDisposition(
        kind=we.WORKER_ONLY, target="t", why="w",
        pending_goal="g-306-211").pending_goal == "g-306-211"
    # Every LIVE row must survive its own validator. This asserted the one pending
    # row (`prime`, ) BY NAME until  shipped that disposition;
    # a by-name assertion would now pin a value the table deliberately no longer
    # carries. Quantifying over the table keeps the validator under test whether
    # or not any row is currently pending, and still fails loudly if a future row
    # is added with junk in the field — which is the property the by-name form
    # was standing in for.
    for stage, d in we.LIFECYCLE_DISPOSITIONS.items():
        assert d.pending_goal is None or re.fullmatch(r"g-\d+-\d+", d.pending_goal), (
            f"{stage!r} carries a malformed pending_goal {d.pending_goal!r}")


@pytest.mark.parametrize("phase", sorted(set(we.WORKER_PHASES) | set(we.REDUCER_ONLY_PHASES)))
def test_every_live_phase_declares_a_lifecycle_stage(phase):
    """The coupling with teeth: PHASE_LIFECYCLE_STAGE is keyed off the tables the
    LIVE gate consumes, so adding a phase without declaring its lifecycle
    disposition fails here (and at import) rather than at 3am."""
    stage = we.PHASE_LIFECYCLE_STAGE.get(phase)
    assert stage is not None, (
        f"phase {phase!r} is in the live phase gate but declares no lifecycle "
        f"stage — add it to PHASE_LIFECYCLE_STAGE")
    assert stage in we.CANONICAL_LIFECYCLE_STAGES


def test_worker_phases_map_to_non_reducer_only_stages():
    """A phase the worker RUNS cannot sit under a reducer-only-by-design stage.

    Without this, the two tables could contradict each other and both
    completeness checks would still pass — each is internally consistent."""
    for phase in we.WORKER_PHASES:
        stage = we.PHASE_LIFECYCLE_STAGE[phase]
        d = we.LIFECYCLE_DISPOSITIONS[stage]
        assert d.kind != we.REDUCER_ONLY_BY_DESIGN, (
            f"phase {phase!r} is in WORKER_PHASES but its lifecycle stage "
            f"{stage!r} is declared reducer-only-by-design — the tables disagree")


def test_reducer_only_phases_map_to_reducer_only_stages():
    """The inverse. `spark` is the deliberate exception and is asserted as one:
    the worker does not run the spark PHASE, but it does perform the CAPTURE
    half, so its stage is worker-only by design (g-306-176)."""
    for phase in we.REDUCER_ONLY_PHASES:
        stage = we.PHASE_LIFECYCLE_STAGE[phase]
        d = we.LIFECYCLE_DISPOSITIONS[stage]
        if phase == "spark":
            assert d.kind == we.WORKER_ONLY, (
                "spark's lifecycle twin is the capture half, which IS worker-only; "
                "if this changed, the g-306-176 hand-off changed with it")
            continue
        assert d.kind == we.REDUCER_ONLY_BY_DESIGN, (
            f"phase {phase!r} is reducer-only but its stage {stage!r} is "
            f"declared {d.kind}")


# --------------------------- the contract has teeth ---------------------------

def test_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="unknown disposition kind"):
        we.LifecycleDisposition(kind="maybe", target="x", why="y")


def test_empty_target_is_refused():
    with pytest.raises(ValueError, match="target must be non-empty"):
        we.LifecycleDisposition(kind=we.WORKER_ONLY, target="   ", why="y")


def test_empty_rationale_is_refused():
    """A row with no `why` is a GAP wearing a disposition's clothes — it names a
    kind without ever stating what makes that kind the right one."""
    with pytest.raises(ValueError, match="why must be non-empty"):
        we.LifecycleDisposition(kind=we.SHARED_COMPONENT, target="x", why="")


def test_scoped_call_without_a_mode_is_refused():
    """This is the no-transcription rule expressed in the type.

    A scoped-call MUST name the mode/flag INSIDE the shared component. Without
    one it is indistinguishable from 'the worker does its own version of this',
    which is precisely the transcription the contract forbids."""
    with pytest.raises(ValueError, match="mode is REQUIRED"):
        we.LifecycleDisposition(kind=we.SCOPED_CALL, target="prime", why="y")


def test_mode_on_a_non_scoped_call_is_refused():
    with pytest.raises(ValueError, match="mode is meaningful only for scoped-call"):
        we.LifecycleDisposition(
            kind=we.WORKER_ONLY, target="x", why="y", mode="light")


def test_gap_detection_actually_fires_when_a_stage_is_undeclared():
    """The load-bearing negative: remove a row and the checker must SAY SO.

    Everything above tests a table that is already complete, so none of it can
    distinguish a working checker from `return []`. This mutates the live table,
    asserts the gap is reported, and restores it."""
    stage = "compact-restore"
    saved = we.LIFECYCLE_DISPOSITIONS.pop(stage)
    try:
        gaps = we.lifecycle_gaps()
        assert any(stage in g and "NO disposition row" in g for g in gaps), gaps
    finally:
        we.LIFECYCLE_DISPOSITIONS[stage] = saved
    assert we.lifecycle_gaps() == []


def test_gap_detection_fires_for_an_undeclared_phase():
    saved = we.PHASE_LIFECYCLE_STAGE.pop("verify")
    try:
        gaps = we.lifecycle_gaps()
        assert any("verify" in g and "no lifecycle stage" in g for g in gaps), gaps
    finally:
        we.PHASE_LIFECYCLE_STAGE["verify"] = saved
    assert we.lifecycle_gaps() == []


# --------------------------- the CLI surface ---------------------------

def _cli(*argv):
    return subprocess.run(
        [sys.executable, str(CORE_SCRIPTS / "worker_execute.py"), *argv],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT), timeout=120)


def test_cli_lifecycle_gaps_exits_0_when_complete():
    """/verify-learning shells out to exactly this, so its exit code is contract."""
    r = _cli("lifecycle-gaps")
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "complete" in r.stdout


def test_cli_lifecycle_prints_every_canonical_stage():
    r = _cli("lifecycle")
    assert r.returncode == 0, r.stderr
    for stage in we.CANONICAL_LIFECYCLE_STAGES:
        assert stage in r.stdout, f"{stage!r} missing from `lifecycle` output"


def test_cli_lifecycle_marks_pending_dispositions(capsys):
    """A DECLARED-but-UNBUILT row must be visibly pending in the human surface.

    The pending marker is what keeps the table from reading, in the indicative,
    as a claim that the code already honours every row.

    This asserted the marker THROUGH the one live pending row (`prime`,
    g-306-211) until 2026-08-16. The prior docstring set the exit condition
    itself — "if that goal lands and the marker is removed, this test is the
    prompt to check that it was removed because the work SHIPPED, not because
    the row was tidied." It shipped: g-306-298 landed the per-unit tier the
    g-306-211 user-directive addendum asked for (worker-loop Phase -0.5 now runs
    rb --recent + the guardrail index ahead of the light-prime-done sentinel, and
    reads the Program in the identity half), verified by that goal's four greps.

    So the subject is gone, and a by-name assertion would silently test nothing.
    The marker MECHANISM is pinned against a SYNTHETIC table instead — that keeps
    the protection alive for the next declared-but-unbuilt row rather than
    letting it lapse together with its last subject, which is the failure mode
    this suite's own header calls "indistinguishable from `return True`"."""
    synthetic = dict(we.LIFECYCLE_DISPOSITIONS)
    stage = we.CANONICAL_LIFECYCLE_STAGES[0]
    synthetic[stage] = we.LifecycleDisposition(
        kind=we.WORKER_ONLY, target="t", why="w", pending_goal="g-999-01")
    with mock.patch.object(we, "LIFECYCLE_DISPOSITIONS", synthetic):
        assert we._main(["lifecycle"]) == 0
    assert "[PENDING g-999-01]" in capsys.readouterr().out
    # ...and the LIVE table's pending set is pinned by name, so a row cannot be
    # declared-but-unbuilt without someone editing this line.
    #
    # This assertion read `== []` until 2026-09-03, and the tripwire worked
    # exactly as its comment promised:  added `verify-own-unit` and
    # this flipped red on the first run. The prompt it carried — "confirm the
    # marker renders for it too" — was then discharged against the real CLI:
    #   verify-own-unit  scoped-call(aspirations-verify, mode=...)  [PENDING ]
    # so the marker is verified for the live row, not merely for the synthetic
    # one above.
    #
    # KEEP THIS BY-NAME rather than relaxing to "non-empty". A count-or-presence
    # assertion would let the NEXT declared-but-unbuilt row slip in silently,
    # which is precisely the protection the 2026-08-16 rewrite existed to keep
    # alive when its last subject shipped.
    assert sorted(
        s for s, d in we.LIFECYCLE_DISPOSITIONS.items() if d.pending_goal
    ) == ["verify-own-unit"]
    assert we.LIFECYCLE_DISPOSITIONS["verify-own-unit"].pending_goal == "g-306-417"


def test_existing_phase_cli_surfaces_unchanged():
    """The lifecycle work must not perturb the surfaces the worker-loop skill
    already calls on every pass."""
    assert _cli("phases").stdout.split() == list(we.WORKER_PHASES)
    assert _cli("reducer-only-phases").stdout.split() == sorted(we.REDUCER_ONLY_PHASES)
