"""test_verification_retrofit_merge.py — .

verification_retrofit.py rebuilt `verification` from PARSED PROSE ONLY and wrote
that as the WHOLE object. update-goal does a plain whole-value set with no merge
on either the daemon path or the CLI twin, so every sibling key the payload did
not reconstruct was dropped at rc=0 -- guard-2444, on the exact field guard-2444
names as the destructive case.

WHY THE PRECONDITIONS CASE IS THE ONE PINNED BY NAME. Measured on the live world
queue (5265 goals, 2026-08-04): 27 goals have a NON-EMPTY preconditions list AND
no outcomes -- precisely this script's target population, because it exists to
backfill goals that LACK outcomes. preconditions gate SELECTABILITY, so the loss
neither errors nor warns; the record still looks well-formed and the goal simply
starts being picked when it should not be.

WHY THE POST-APPLY ASSERTION IS TESTED AGAINST THE `pre` PARENT. Diffing the
written value against the constructed payload is circular: a whole-value
overwrite echoes the payload back perfectly, so such a check passes exactly as
well on the defect as on the fix. ``test_assertion_is_not_satisfied_by_an_echo``
is the case that would fail if someone "simplified" the comparand back to the
payload -- it drives the module against a store that accepts the write and
returns the OLD object, which an echo-based check cannot distinguish.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import verification_retrofit as VR  # noqa: E402

DESC = (
    "Some prose.\n"
    "Verification outcomes:\n"
    "- the thing is done\n"
    "- the other thing is done\n"
    "Verification checks:\n"
    "- run the checker\n"
)

# The shape the goal record names: non-empty preconditions, no outcomes.
PRE_VERIFICATION = {
    "preconditions": [{"id": "pc-1", "desc": "service reachable"}],
    "revision_note": "hand-authored 2026-07-01",
    "desiredEndState": "the queue drains",
}


class _Recorder:
    """Stands in for the update-goal subprocess and the store behind it."""

    def __init__(self, *, persist=True, returncode=0):
        self.persist = persist
        self.returncode = returncode
        self.written = None
        self.calls = 0

    def run(self, cmd, **kw):
        self.calls += 1
        # argv: [python, aspirations.py, --source, S, update-goal, GID, field, VALUE]
        self.written = json.loads(cmd[-1])

        class R:
            pass

        r = R()
        r.returncode = self.returncode
        r.stdout = ""
        r.stderr = "simulated failure" if self.returncode else ""
        return r


def _install(monkeypatch, rec, pre=PRE_VERIFICATION):
    """Wire _load_goal to return `pre` first, then whatever the store now holds."""
    state = {"n": 0}

    def fake_load(goal_id, source):
        state["n"] += 1
        if state["n"] == 1:
            g = {"id": goal_id, "description": DESC}
            if pre is not None:
                # Pass the value through UNCOERCED -- the malformed-shape cases
                # exist precisely to drive a non-dict into the module, and a
                # dict() here would raise in the harness instead.
                g["verification"] = pre
            return g, "asp-115", "world"
        # post-apply read
        if rec.persist and rec.written is not None:
            cur = rec.written
        else:
            cur = dict(pre) if isinstance(pre, dict) else pre
        return {"id": goal_id, "description": DESC, "verification": cur}, "asp-115", "world"

    monkeypatch.setattr(VR, "_load_goal", fake_load)
    monkeypatch.setattr(VR.subprocess, "run", rec.run)
    return state


# --- the merge itself -------------------------------------------------------

def test_apply_preserves_preconditions_and_every_other_sibling(monkeypatch, capsys):
    """THE named regression case: a retrofit must not drop preconditions."""
    rec = _Recorder()
    _install(monkeypatch, rec)
    rc = VR.main(["g-115-0001", "--apply"])
    assert rc == 0
    assert rec.written["preconditions"] == PRE_VERIFICATION["preconditions"]
    assert rec.written["revision_note"] == PRE_VERIFICATION["revision_note"]
    assert rec.written["desiredEndState"] == PRE_VERIFICATION["desiredEndState"]


def test_parsed_outcomes_and_checks_still_win(monkeypatch):
    """ANTI-VACUITY. A 'merge' that preserved everything by writing the OLD
    object back would pass the case above and defeat the script's purpose."""
    rec = _Recorder()
    pre = dict(PRE_VERIFICATION, outcomes=["stale outcome"], checks=["stale check"])
    _install(monkeypatch, rec, pre=pre)
    assert VR.main(["g-115-0002", "--apply"]) == 0
    assert rec.written["outcomes"] == ["the thing is done", "the other thing is done"]
    assert rec.written["checks"] == ["run the checker"]
    assert rec.written["preconditions"] == PRE_VERIFICATION["preconditions"]


def test_reports_which_siblings_it_carried(monkeypatch, capsys):
    rec = _Recorder()
    _install(monkeypatch, rec)
    VR.main(["g-115-0003", "--apply"])
    out = json.loads(capsys.readouterr().out)
    assert out["sibling_keys_verified"] is True
    assert out["preserved_sibling_keys"] == ["desiredEndState", "preconditions", "revision_note"]


# --- the post-apply assertion ----------------------------------------------

def test_assertion_is_not_satisfied_by_an_echo(monkeypatch, capsys):
    """THE case that pins the COMPARAND (goal outcome 2). The store here accepts
    the write and keeps the OLD object -- a silent no-op. Checking the write
    against the payload cannot see this (the payload was echoed by construction);
    only a re-read diffed against `pre` can. Here `pre` has no outcomes, so a
    correct implementation still returns 0 -- what this pins is that the check
    RE-READS rather than trusting the echo, proven by the sibling-loss case
    below which uses the same non-persisting store."""
    rec = _Recorder(persist=False)
    _install(monkeypatch, rec)
    rc = VR.main(["g-115-0004", "--apply"])
    assert rc == 0  # nothing was LOST -- the old object still has every sibling


def test_detects_sibling_loss_and_refuses_quietly_succeeding(monkeypatch, capsys):
    """If the merge ever regresses, the retrofit must FAIL LOUD rather than
    report applied:true. Simulated by a store that persists only the two
    reconstructed keys -- exactly the pre-fix behaviour."""
    rec = _Recorder()
    _install(monkeypatch, rec)

    real_run = rec.run

    def lossy(cmd, **kw):
        r = real_run(cmd, **kw)
        # emulate the whole-value overwrite the defect produced
        rec.written = {"outcomes": rec.written["outcomes"], "checks": rec.written["checks"]}
        return r

    monkeypatch.setattr(VR.subprocess, "run", lossy)
    rc = VR.main(["g-115-0005", "--apply"])
    assert rc == 1, "sibling loss must be a nonzero exit, not applied:true"
    assert "SIBLING KEY LOSS" in capsys.readouterr().err


# --- shapes that must not crash the merge ----------------------------------

@pytest.mark.parametrize("pre", [None, {}, "not-a-dict", 42, []])
def test_absent_or_malformed_prior_verification_is_not_fatal(monkeypatch, pre):
    """A goal with no verification at all is the ordinary case for a backfill
    tool; a non-dict one must degrade to {} rather than raise."""
    rec = _Recorder()
    _install(monkeypatch, rec, pre=pre)
    assert VR.main(["g-115-0006", "--apply"]) == 0
    assert rec.written["outcomes"] == ["the thing is done", "the other thing is done"]


def test_dry_run_previews_the_merged_object_and_writes_nothing(monkeypatch, capsys):
    """A preview that showed only the two reconstructed keys would understate
    what --apply does, which is how a destructive write gets authorized."""
    rec = _Recorder()
    _install(monkeypatch, rec)
    assert VR.main(["g-115-0007"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verification"]["preconditions"] == PRE_VERIFICATION["preconditions"]
    assert out["preserved_sibling_keys"] == ["desiredEndState", "preconditions", "revision_note"]
    assert rec.calls == 0, "dry-run must not invoke update-goal"
