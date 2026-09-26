""": the unblock-parent sweep and an Unblock a Body CLAIMED.

THE INCIDENT (2026-08-24, relayed from g-306-362). A Body claimed Unblock
g-306-362 at 00:32:40 and rewrote its parent's interval_hours from 1.78 to 10
at 00:35:36. At 00:43:34 this sweep marked it skipped with "parent resolved
without action needed", computing that verdict against the value the Body had
written 8 minutes earlier. Two defects: it terminated a claimed goal out from
under a live Body, and it recorded that Body's own fix as no action.

WHAT WAS ALREADY FIXED, AND WHAT THIS FILE ADDS. g-115-7410 (d23d9c7f7a, the
same day) taught the shared write guard to refuse any goal whose
claimed_by_sid is set, and appended a caveat to the note. That closed the
live-claim half; test_sweep_write_guard.py pins it with a stubbed re-read.
Two things stayed open, and they are pinned here:

  1. THE STALE-CLAIM CONTROL. The guard refuses EVERY claim, live or dead,
     because liveness is not this sweep's call: a live Body that has written no
     diary entry reads exactly like a dead one (guard-2715). So a dead claim
     reaches this sweep only after its liveness owner, stranded-claim-sweep,
     RELEASES it. The test drives that release through the owner's own
     `_release_goal`. If a release ever stopped clearing the claim, every such
     Unblock would become immortal, and that test goes red.
  2. THE NOTE. After a release the goal still carries its execution history
     (started / executed_by / executed_by_sid survive a release), so "without
     action needed" is contradicted by the record itself. The worked note
     credits the work and keeps the executor's own outcome_note.

MUTATION OUTCOMES, stated before running (guard-4166) and then measured on a
scratch worktree (g-306-510):
  - `if worked:` branch deleted: every worked-note test, the stale-claim test
    and the incident mirror go RED. The unworked control, the dedup test and
    the live-claim test stay GREEN. Measured: exactly that, 6 red / 3 green.
  - `_sweep_write_guard.ACTIVE_CLAIM_FIELDS = ()`: the live-claim test and the
    stale-claim test's first leg go RED. Measured: exactly those 2.
  - The PRESERVED append deleted: predicted the preservation test and the
    incident mirror. Measured 3 red: the retry test too, because it counts the
    PRESERVED block and a note without one counts zero.
  - The retry-reuse branch deleted: the retry test alone goes RED (measured).
"""

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
for _p in (str(TESTS_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _daemon_fixture import DaemonFixture  # noqa: E402
from test_unblock_parent_status_sweep_integration import (  # noqa: E402
    _make_world_with_pair,
    _read_goal,
    _run_sweep,
)

OLD_PREFIX = "parent resolved without action needed"
SID = "1dc6fc35-c568-4912-8ef3-1cf10b102721"
EXECUTOR_NOTE = ("OUTCOME 1: MET — parent interval_hours rewritten 1.78 -> 10, "
                 "so its cadence reads healthy again. Source: update-goal rc 0.")


def _load(stem, alias):
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS_DIR / f"{stem}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load("unblock-parent-status-sweep", "unblock_parent_worked")


def _stamp(**delta):
    return (dt.datetime.now() - dt.timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S")


def _written_note(mod, goal):
    """Run `_mark_skipped` against `goal` as the store-of-record read and return
    the outcome_note it WROTE (the value handed to update-goal)."""
    mod._reread_goal_authoritative = lambda s, g: (goal, mod.PROV_AUTHORITATIVE)
    calls = []
    mod._py = lambda args, input_text=None: (calls.append(args), (0, "", ""))[1]
    assert mod._mark_skipped("world", "g-306-362", "g-306-284", "completed") is True
    assert calls[0][-3:-1] == ["g-306-362", "outcome_note"], calls[0]
    return calls[0][-1]


WORKED = {"id": "g-306-362", "status": "pending",
          "started": "2026-08-24T00:32:40", "executed_by": "alpha",
          "executed_by_sid": SID}


# ---------------------------------------------------------------------------
# The note (stubbed store read — fast)
# ---------------------------------------------------------------------------

def test_a_worked_unblock_is_not_noted_as_needing_no_action(mod):
    note = _written_note(mod, dict(WORKED))
    assert "without action needed" not in note, note
    assert note.startswith(mod._WORKED_NOTE_PREFIX), note
    assert "parent_id=g-306-284" in note and "parent.status=completed" in note
    assert "executed_by=alpha" in note and f"executed_by_sid={SID}" in note


def test_the_executors_own_outcome_note_is_kept(mod):
    note = _written_note(mod, dict(WORKED, outcome_note=EXECUTOR_NOTE))
    head, sep, kept = note.partition("\n\nPRESERVED prior outcome_note:\n")
    assert sep, f"the executor's note was bare-replaced: {note!r}"
    assert kept == EXECUTOR_NOTE
    assert head.startswith(mod._WORKED_NOTE_PREFIX)


def test_a_retry_after_a_failed_status_write_reuses_its_note(mod):
    """_mark_skipped writes the note, THEN the status. If the status write
    fails, the goal stays open carrying this sweep's note and is re-swept. The
    retry must not wrap its own note in a second PRESERVED block."""
    first = _written_note(mod, dict(WORKED, outcome_note=EXECUTOR_NOTE))
    second = _written_note(mod, dict(WORKED, outcome_note=first))
    assert second == first
    assert second.count("PRESERVED prior outcome_note") == 1


def test_an_earlier_no_action_note_is_replaced_not_preserved(mod):
    """A worked goal can still carry the OLD note from a partial write that
    predates this change. That text is this sweep's own boilerplate, not the
    executor's account, so it must not survive into the new note."""
    old = f"{OLD_PREFIX} (parent_id=g-306-284, parent.status=completed)"
    note = _written_note(mod, dict(WORKED, outcome_note=old))
    assert "without action needed" not in note, note


def test_an_unworked_unblock_keeps_the_original_note(mod):
    """CONTROL — must stay green under every mutation above. An Unblock nobody
    ever claimed has no execution history, and the original note is right."""
    note = _written_note(mod, {"id": "g-306-362", "status": "pending"})
    assert note.startswith(OLD_PREFIX), note


def test_the_worked_note_is_a_dedup_key_too(mod):
    swept = {"outcome_note": f"{mod._WORKED_NOTE_PREFIX} (parent_id=g-1-2)",
             "status": "skipped"}
    assert mod._is_already_swept(swept) is True
    assert mod._is_already_swept(dict(swept, status="pending")) is False


# ---------------------------------------------------------------------------
# Through main(), against a real daemon and a real store
# ---------------------------------------------------------------------------

def _claimed(**extra):
    return dict({"status": "in-progress", "claimed_by": "bravo",
                 "claimed_by_sid": SID, "executed_by": "bravo",
                 "executed_by_sid": SID}, **extra)


def test_a_live_claim_is_not_swept():
    """OUTCOME 1, live half: claimed and in-progress, parent terminal."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(
            Path(tmpd), parent_status="skipped",
            unblock_extra=_claimed(claimed_at=_stamp(minutes=3),
                                   started=_stamp(minutes=3)))
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, err
            assert json.loads(out)["applied"] == 0
            assert "claim in flight" in err, err
            g = _read_goal(world, "g-700-73")
            assert g["status"] == "in-progress"
            assert g.get("claimed_by_sid") == SID
            assert not g.get("outcome_note"), g.get("outcome_note")


def test_a_stale_claim_is_swept_once_its_liveness_owner_releases_it():
    """OUTCOME 1, stale control. A claim four months old whose holder is gone.

    Leg 1: the sweep still refuses. Judging the holder dead is the liveness
    owner's decision, not this sweep's. Leg 2: stranded-claim-sweep's own
    `_release_goal` releases it. Leg 3: now the sweep closes it, and credits the
    work the released claim did."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(
            Path(tmpd), parent_status="skipped",
            unblock_extra=_claimed(claimed_at="2026-05-01T00:05:00",
                                   started="2026-05-01T00:05:00"))
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, err
            assert json.loads(out)["applied"] == 0, "stale claim swept before release"
            assert _read_goal(world, "g-700-73").get("claimed_by_sid") == SID

            scs = _load("stranded-claim-sweep", "stranded_claim_sweep_worked")
            released = scs._release_goal("g-700-73", "world")
            assert released.get("ok") is True, released
            g = _read_goal(world, "g-700-73")
            assert not g.get("claimed_by_sid"), "release left the claim in place"

            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, err
            assert json.loads(out)["applied"] == 1, out
            g = _read_goal(world, "g-700-73")
            assert g["status"] == "skipped"
            note = g.get("outcome_note") or ""
            assert "without action needed" not in note, note
            assert "executed_by=bravo" in note, note


def test_the_canonical_incident_credits_the_bodys_fix():
    """OUTCOME 2, through main. The  shape: a starvation Unblock whose
    recurring parent reads healthy only because the Body rewrote its
    interval_hours. The parent's lastAchievedAt PREDATES the claim, so any
    ordering test would credit nobody. The note must credit the work, and the
    Body's own account must survive."""
    with tempfile.TemporaryDirectory() as tmpd:
        world, agent_dir = _make_world_with_pair(
            Path(tmpd), parent_status="pending",
            parent_extra={"recurring": True, "interval_hours": 10,
                          "lastAchievedAt": _stamp(hours=8)},
            unblock_extra={
                "origin_signal": "unblock:recurring-starved-g-700-69-20260824",
                "title": ("Unblock: recurring goal g-700-69 has stopped firing "
                          "(5.3h = 3.0x its expected cadence)"),
                "started": _stamp(hours=1), "executed_by": "alpha",
                "executed_by_sid": SID, "outcome_note": EXECUTOR_NOTE})
        with DaemonFixture(world):
            rc, out, err = _run_sweep(world, agent_dir, apply=True)
            assert rc == 0, err
            assert json.loads(out)["applied"] == 1, out
            g = _read_goal(world, "g-700-73")
            note = g.get("outcome_note") or ""
            assert g["status"] == "skipped"
            assert "without action needed" not in note, note
            assert "executed_by=alpha" in note, note
            assert note.endswith(EXECUTOR_NOTE), note
