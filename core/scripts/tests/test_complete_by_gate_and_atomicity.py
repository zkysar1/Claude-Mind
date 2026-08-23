"""test_complete_by_gate_and_atomicity.py — .

Pins three properties of the RECURRING closure path
(``mind_api/src/endpoints/aspirations_write.py::complete_by``), which is the
ONLY path a recurring goal has: ``update_goal`` refuses ``status=completed``
for recurring goals and redirects them here, so this handler is not a path they
opt into.

  1. ATOMIC — ``completed_by`` and the achievement counters land TOGETHER or
     not at all. Proven by injecting a failure BETWEEN the two writes and
     asserting the store carries NEITHER.
  2. UNGATED, DELIBERATELY — neither one-shot completion gate runs here, and
     that is a recorded decision rather than an oversight. Pinned so a future
     wholesale import is a deliberate, test-visible change.
  3. THE LOAD-BEARING NEGATIVE — a genuinely-satisfied recurring close still
     succeeds and advances every counter.

── Why property 1 is asserted by INJECTION rather than by a happy-path check ──

The goal's outcome asks for atomicity "proven by a test that fails against the
current code". That is not satisfiable as written, and saying so is part of the
work: the defect it presumes is NOT PRESENT. ``complete_by`` takes one lock,
mutates ``completed_by`` and the whole counter block in memory, and persists via
a SINGLE ``_atomic_write_jsonl``. There is no interior commit point, so no
failure inside the handler can leave a partial record. Re-verified against
today's source (the handler moved from ~L3450 to L4073 since the goal was filed
— re-anchor by name, never by line).

So the honest test is one that WOULD fail against a non-atomic implementation.
Asserting "both fields present after a successful close" is not that test: a
handler that wrote ``completed_by`` first and the counters in a second write
passes it on the happy path, which is exactly the shape the g-326-85 report
described. Injecting a failure into the window between the two writes
discriminates — under a two-write implementation the store keeps the orphaned
``completed_by``; under this one it keeps nothing.

``_nonholder_claim_warning`` is the injection point because of WHERE it sits:
after ``goal["completed_by"] = agent`` and before the recurring branch that
bumps the counters. It is a lever, not the subject.

── What this test does NOT establish ──

The mechanism behind the originally-reported partial write (``completed_by``
present, counters absent on g-326-85) remains UNKNOWN. Abort-inside-the-handler
is eliminated — by the code reading above and by property 1 below — leaving a
post-hoc cross-machine merge or a filing-time transcription error, neither
observed. The specimen is gone: g-326-85 re-cycles every ~40min and has
overwritten the reported state many times over. A defect specimen living in a
mutable, high-frequency record is not evidence anyone can return to.

Hermetic: in-process ``DaemonFixture``, no ``daemon_integration`` marker, so it
runs in the daemon-safe ``-m "not daemon_integration"`` subset.

Run: py -3 -m pytest core/scripts/tests/test_complete_by_gate_and_atomicity.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _daemon_fixture import DaemonFixture  # noqa: E402  (shared in-process daemon)

CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent
for _p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _rt  # noqa: E402  (canonical Python -> daemon client)

# PACKAGE-QUALIFIED ON PURPOSE. _daemon_fixture imports `mind_api.src.server`,
# so the live handler is `mind_api.src.endpoints.aspirations_write`. Importing
# it as `src.endpoints.aspirations_write` (which also resolves, with mind_api/
# on sys.path) yields a DIFFERENT module object, and every monkeypatch below
# would silently patch a copy the daemon never calls.
from mind_api.src.endpoints import aspirations_write as AW  # noqa: E402

GOAL_ID = "g-901-01"


def _seed_world(tmp: Path) -> Path:
    """One recurring goal, claimed and in-progress, with counters at a known
    non-zero baseline so an increment is distinguishable from a re-initialise."""
    world = tmp / "world"
    world.mkdir(exist_ok=True)
    goal = {
        "id": GOAL_ID,
        "title": "Recurring gate/atomicity probe",
        "description": "complete_by gate + atomicity probe",
        "status": "in-progress",
        "priority": "MEDIUM",
        "claimed_by": "alpha",
        "claimed_at": "2026-08-21T00:00:00",
        "blocked_by": [],
        "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
        "origin_signal": "user_directive",
        "participants": ["agent"],
        "recurring": True,
        "interval_hours": 24,
        "lastAchievedAt": None,
        "achievedCount": 7,
        "currentStreak": 3,
        "longestStreak": 5,
        "windowStreak": 2,
        "longestWindowStreak": 4,
        "windowStreakMultiplier": 7,
    }
    asp = {
        "id": "asp-901", "title": "Test asp", "motivation": "Test",
        "scope": "project", "priority": "MEDIUM", "status": "active",
        "created": "2026-08-21T00:00:00", "goals": [goal],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _read_goal(world: Path) -> dict:
    for line in (world / "aspirations.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for g in json.loads(line).get("goals", []):
            if g.get("id") == GOAL_ID:
                return g
    raise KeyError(GOAL_ID)


# ── 3. the load-bearing negative, first because everything else leans on it ──

def test_a_genuinely_satisfied_recurring_close_still_succeeds():
    """THE LOAD-BEARING NEGATIVE (the goal's fourth outcome).

    Every other assertion here is about something NOT happening — no partial
    write, no gate call — and "nothing happened" is also what a handler that
    refuses everything produces. This test is the control that keeps the rest
    meaningful: the ordinary close must still work, and every counter must
    advance in the same call that stamps ``completed_by``.
    """
    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world):
            _rt.aspirations_complete_by(GOAL_ID, source="world", agent_name="alpha")
        g = _read_goal(world)

        assert g["completed_by"] == "alpha"
        assert g["status"] == "pending", "a recurring goal cycles back, never terminal"
        assert g["achievedCount"] == 8, "7 -> 8"
        assert g["currentStreak"] == 4, "3 -> 4"
        assert g["longestStreak"] == 5, "unchanged: currentStreak 4 has not passed it"
        assert g["windowStreak"] == 3, "2 -> 3"
        assert g["lastAchievedAt"] is not None
        assert "claimed_by" not in g, "the claim must be released so it re-selects"


# ── 1. atomicity ─────────────────────────────────────────────────────────────

def test_a_failure_between_the_two_writes_leaves_neither(monkeypatch):
    """ATOMICITY, the discriminating form.

    ``_nonholder_claim_warning`` runs AFTER ``goal["completed_by"] = agent`` and
    BEFORE the recurring branch bumps the counters. Raising there produces
    exactly the state the g-326-85 report described — ``completed_by`` set,
    counters not — in memory. The assertion is that NONE of it reaches disk.

    Against a two-write implementation this test goes red: the first write would
    already have landed the orphaned ``completed_by``. That is what makes it a
    guard rather than a restatement of the happy path.
    """
    fired = {"n": 0}

    def _boom(*a, **k):
        fired["n"] += 1
        raise RuntimeError("injected failure between completed_by and the counters")

    monkeypatch.setattr(AW, "_nonholder_claim_warning", _boom)

    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world):
            with pytest.raises(Exception):
                _rt.aspirations_complete_by(GOAL_ID, source="world", agent_name="alpha")
        g = _read_goal(world)

    # ANTI-VACUITY: if the patch did not reach the module the daemon actually
    # calls, the close would have SUCCEEDED and the assertions below would pass
    # for entirely the wrong reason (a successful close leaves no partial state
    # either). This is the check that distinguishes the two.
    assert fired["n"] > 0, (
        "the injection never ran — the monkeypatch targeted a module object the "
        "daemon does not call, so this test measured nothing")

    assert "completed_by" not in g, (
        "an ORPHANED completed_by reached disk without its counters: the handler "
        "committed twice. This is the g-326-85 partial-write shape, and the whole "
        "point of the single-lock/single-write structure is that it cannot happen")
    assert g["achievedCount"] == 7, "counters must be untouched by a failed close"
    assert g["currentStreak"] == 3
    assert g["status"] == "in-progress", "a failed close must not cycle the goal"
    assert g["claimed_by"] == "alpha", "a failed close must not release the claim"


# ── 2. ungated, deliberately ─────────────────────────────────────────────────

def test_neither_one_shot_completion_gate_runs_on_the_recurring_path(monkeypatch):
    """DEFECT A, pinned as a DECISION rather than reported as a bug.

    ``update_goal`` runs ``uncommitted_work`` and ``completion_artifact`` before
    a terminal close. ``complete_by`` runs neither. The g-115-5156 review
    decided NOT to import them wholesale, and the reason is the operator's own
    design invariant for this path — *a refusal must let the agent MOVE ON; it
    must never wedge the loop*:

      * ``uncommitted_work`` blocks on a dirty framework tree. Applied here it
        would wedge EVERY recurring smoke test and audit sweep on any box with
        one uncommitted ``core/`` file — i.e. precisely the heartbeat goals this
        path exists to serve.
      * ``completion_artifact`` is mis-shaped for a sweep: a sweep that
        correctly returns "0 findings" has no artifact, and a clean sweep's zero
        IS the win (``learning-philosophy.md``).

    The open recommendation is a RECURRING-SPECIFIC check (did this cycle do
    anything?), not these two. This test does not endorse "ungated forever" — it
    makes the current state deliberate, so whoever imports a gate here has to
    come through this assertion and its reasoning first.
    """
    calls: list[str] = []
    ran = {"handler": 0}

    real_nonholder = AW._nonholder_claim_warning

    def _spy_nonholder(*a, **k):
        ran["handler"] += 1
        return real_nonholder(*a, **k)

    monkeypatch.setattr(AW, "_nonholder_claim_warning", _spy_nonholder)
    monkeypatch.setattr(AW, "_uncommitted_work_eval",
                        lambda *a, **k: calls.append("uncommitted_work"))
    monkeypatch.setattr(AW, "_completion_artifact_eval",
                        lambda *a, **k: calls.append("completion_artifact"))

    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world):
            _rt.aspirations_complete_by(GOAL_ID, source="world", agent_name="alpha")
        g = _read_goal(world)

    # ANTI-VACUITY, same reason as above: "no gate fired" is also what a
    # patch that never reached the daemon's module produces. The spy on a
    # function that IS called proves the patching worked.
    assert ran["handler"] > 0, (
        "the control spy never fired — the monkeypatches did not reach the "
        "module the daemon calls, so the gate assertion below measures nothing")

    assert calls == [], (
        f"complete_by invoked one-shot completion gate(s) {calls}. If that is "
        "intended, read this test's docstring first: importing uncommitted_work "
        "here wedges every recurring sweep on a box with a dirty core/ tree, "
        "which the design invariant for this path forbids.")
    assert g["achievedCount"] == 8, "and the close still succeeded"


# ── a characterization test, labelled as one ─────────────────────────────────

def test_write_loss_detector_does_not_yet_cover_the_counters(monkeypatch):
    """CHARACTERIZATION, not endorsement — this pins a KNOWN BLIND SPOT.

    The never-success-without-persistence check (g-115-2429) re-reads the store
    after the write and refuses to report success if the transition is missing.
    It verifies ``status`` and ``lastAchievedAt`` only. ``achievedCount``,
    ``currentStreak`` and ``windowStreak`` are NOT in its expectation set, so a
    write that landed the status while losing the counters would still report
    success.

    This is recorded in code rather than only in a goal note because it bears
    directly on why the g-326-85 mechanism stayed unknown: the detector also
    runs IMMEDIATELY after the write, so a cross-machine merge landing later
    could not be caught by it at any width.

    WIDENING IT IS WELCOME. If you add the counters to the expectation set, this
    test SHOULD go red — update it, do not delete it.
    """
    seen: dict = {}
    real = AW._verify_transition_persisted

    def _spy(live_path, asp_id, goal_id, expected):
        seen["expected"] = dict(expected)
        return real(live_path, asp_id, goal_id, expected)

    monkeypatch.setattr(AW, "_verify_transition_persisted", _spy)

    with tempfile.TemporaryDirectory() as tmpd:
        world = _seed_world(Path(tmpd))
        with DaemonFixture(world):
            _rt.aspirations_complete_by(GOAL_ID, source="world", agent_name="alpha")

    assert seen, "the persistence detector did not run at all"
    assert set(seen["expected"]) == {"status", "lastAchievedAt"}, (
        f"the detector's expectation set changed to {sorted(seen['expected'])}. "
        "If counters were ADDED, that is an improvement — update this test's "
        "docstring and assertion rather than reverting the widening.")
