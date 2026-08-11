"""test_recurring_precondition_sweep_shelve_trace.py —  regression.

recurring-precondition-sweep.py advances `lastAchievedAt` on a recurring goal
whose structured precondition fails, so overdue_ratio stops inflating. It has
NEVER touched `achievedCount` — so that advance is a SHELVE, not an achievement.

The defect this pins: after the advance, the goal record was INDISTINGUISHABLE
from one that genuinely closed. `lastAchievedAt` reads fresh, and
`last_outcome_origin` still holds whatever the last REAL close wrote
(recurring-close.sh is its only writer), so a stale "genuine" sits beside a
fresh timestamp. Discriminating needed TWO readings of `achievedCount` across
time; a single read could not do it.

Measured on g-115-15 (2026-07-31): `lastAchievedAt` advanced ~40h (>=5 intervals
at 7.995h) while `achievedCount` sat at 91, and a goal addendum written off that
record inferred "roughly 11 genuine deep closes with zero span evidence" from
what was in fact zero closes. Same class as rb-245 — a field that MOVES read as
proof of an event a different field says did not happen.

Fix under test: every shelve writes `last_shelved_at` (== the advance timestamp)
and `last_shelve_reason` ("<gate_kind>:<predicate type>"), giving the
single-read discriminator `lastAchievedAt == last_shelved_at => shelved`.

DELIBERATELY pytest-VISIBLE (top-level `def test_`). Its sibling
test_recurring_precondition_sweep_fire_when.py is a `main()`-style file pytest
collects ZERO tests from, so a green `pytest` run says nothing about it — the
exact blind spot run-full-suite-after-deep-code.md names. New coverage for this
script should be collectable.

Hermetic: `_update_goal_field` is monkeypatched, so no write of any kind
reaches a real store — no --dry-run needed, and the write path is still
exercised as main() would call it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
SWEEP_PY = CORE_SCRIPTS / "recurring-precondition-sweep.py"

sys.path.insert(0, str(CORE_SCRIPTS))

from _paths import agent_dir as _agent_dir  # noqa: E402


def _load_sweep_module():
    """Import the hyphenated sweep script under a clean module name."""
    spec = importlib.util.spec_from_file_location("_rps_trace_under_test", SWEEP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).replace(microsecond=0).isoformat()


@pytest.fixture()
def sweep_agent(monkeypatch):
    """A temp agent dir at the real PROJECT_ROOT holding one shelve-eligible goal.

    _paths.py derives PROJECT_ROOT from script location and is NOT overridable,
    so the test agent must live at the real PROJECT_ROOT (same constraint the
    fire_when sibling documents).

    Binds MIND_AGENT to this temp agent: _source_paths() reads that var at CALL
    time to decide which agent queue to sweep. Without the bind the sweep scans
    whatever agent the session is bound to (the PreToolUse hook injects a real
    one), the fixture goal is never seen, and every assertion here fails — or,
    worse, passes vacuously.
    """
    agent_name = f"_shelve-trace-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("MIND_AGENT", agent_name)
    adir = _agent_dir(agent_name)
    adir.mkdir(parents=True, exist_ok=True)
    missing = str(adir / "definitely-not-here.sentinel")
    asp = {
        "id": "asp-test",
        "title": "Test",
        "status": "active",
        "scope": "maintenance",
        "goals": [
            {
                "id": "g-shelve-me",
                "title": "Recurring with failing precondition, past gate",
                "status": "pending",
                "priority": "MEDIUM",
                "recurring": True,
                "interval_hours": 24,
                "lastAchievedAt": _hours_ago_iso(48),
                "achievedCount": 91,
                "last_outcome_origin": "genuine",
                "verification": {
                    "preconditions": [
                        {"type": "file_check", "path": missing, "condition": "exists"}
                    ]
                },
            }
        ],
    }
    (adir / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
    yield adir
    import shutil

    shutil.rmtree(adir, ignore_errors=True)


@pytest.fixture()
def make_sweep_agent(monkeypatch):
    """Factory form of `sweep_agent` — builds a temp agent queue holding any goals.

    The single-goal fixture above cannot express the gate-collision and
    fire_when-only shapes, which need different predicate sets per test.
    Same PROJECT_ROOT and MIND_AGENT constraints apply; see that fixture.
    """
    created: list[Path] = []

    def _make(goals: list[dict]) -> Path:
        agent_name = f"_shelve-trace-test-{uuid.uuid4().hex[:8]}"
        monkeypatch.setenv("MIND_AGENT", agent_name)
        adir = _agent_dir(agent_name)
        adir.mkdir(parents=True, exist_ok=True)
        created.append(adir)
        asp = {
            "id": "asp-test", "title": "Test", "status": "active",
            "scope": "maintenance", "goals": goals,
        }
        (adir / "aspirations.jsonl").write_text(json.dumps(asp) + "\n", encoding="utf-8")
        return adir

    yield _make
    import shutil

    for d in created:
        shutil.rmtree(d, ignore_errors=True)


def _recurring_goal(goal_id: str, *, preconditions=None, fire_when=None) -> dict:
    """A recurring goal already past its time gate (48h elapsed on a 24h interval)."""
    g = {
        "id": goal_id,
        "title": "Recurring, past gate",
        "status": "pending",
        "priority": "MEDIUM",
        "recurring": True,
        "interval_hours": 24,
        "lastAchievedAt": _hours_ago_iso(48),
        "achievedCount": 91,
        "last_outcome_origin": "genuine",
    }
    if preconditions is not None:
        g["verification"] = {"preconditions": preconditions}
    if fire_when is not None:
        g["fire_when"] = fire_when
    return g


# A path that certainly exists (this test file) and one that certainly does not.
_PRESENT = str(Path(__file__).resolve())
_ABSENT = str(Path(__file__).resolve().parent / "definitely-not-here-shelve-trace.sentinel")


def _run_sweep_capturing_writes(monkeypatch, mod):
    """Run main() with the field writer recorded instead of executed."""
    calls: list[tuple] = []

    def fake_update(goal_id, source, field, value, dry_run):
        calls.append((goal_id, field, value, dry_run))
        return True

    monkeypatch.setattr(mod, "_update_goal_field", fake_update)
    monkeypatch.setattr(sys, "argv", ["recurring-precondition-sweep.py"])
    rc = mod.main()
    return rc, calls


def test_shelve_writes_trace_fields(monkeypatch, sweep_agent):
    """A shelve writes lastAchievedAt AND both trace fields for the same goal."""
    mod = _load_sweep_module()
    rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    assert rc == 0

    ours = [c for c in calls if c[0] == "g-shelve-me"]
    assert ours, (
        "the shelve-eligible goal was never written — sweep did not advance it; "
        f"all writes seen: {calls}"
    )
    fields = {c[1]: c[2] for c in ours}
    assert "lastAchievedAt" in fields, "the advance itself must still happen"
    assert "last_shelved_at" in fields, (
        "shelve trace missing: without last_shelved_at a reader cannot tell a "
        "shelve from a genuine close in a SINGLE read of the goal record"
    )
    assert "last_shelve_reason" in fields


def test_shelved_at_equals_advance_timestamp(monkeypatch, sweep_agent):
    """The single-read discriminator: lastAchievedAt == last_shelved_at.

    If these ever diverge the discriminator silently stops working — a reader
    would conclude "the last movement was an achievement" on a shelved goal,
    which is the exact misreading this fix exists to prevent.
    """
    mod = _load_sweep_module()
    _rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    fields = {c[1]: c[2] for c in calls if c[0] == "g-shelve-me"}
    assert fields["lastAchievedAt"] == fields["last_shelved_at"]


def test_shelve_reason_names_the_failing_gate(monkeypatch, sweep_agent):
    """The reason records gate kind + predicate type, so 'why' needs no re-eval."""
    mod = _load_sweep_module()
    _rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    fields = {c[1]: c[2] for c in calls if c[0] == "g-shelve-me"}
    reason = fields["last_shelve_reason"]
    assert reason.startswith("precondition:"), reason
    assert "file_check" in reason, reason


def test_achieved_count_is_never_written(monkeypatch, sweep_agent):
    """The load-bearing invariant: a shelve must NOT look like an achievement.

    Guards the whole premise — if the sweep ever incremented achievedCount, the
    shelve would become genuinely indistinguishable from a close and no trace
    field could recover the difference.
    """
    mod = _load_sweep_module()
    _rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    ours = [c for c in calls if c[0] == "g-shelve-me"]
    # NON-VACUITY GUARD. Without this the assertions below pass trivially when
    # the sweep wrote NOTHING — which is exactly what happened on this test's
    # first run (the fixture goal was not being discovered, so "achievedCount
    # was not written" was true because no field was written at all). A
    # never-fails check reads as confirmation; see rb-245 / guard-1802.
    assert ours, "sweep wrote nothing for the fixture goal — assertions below would be vacuous"
    written = {c[1] for c in ours}
    assert "achievedCount" not in written
    assert "currentStreak" not in written
    assert "last_outcome_origin" not in written, (
        "last_outcome_origin belongs to recurring-close.sh (genuine|forced); "
        "overloading it here would collide with the precheck's reading of it"
    )


# ── : gate_kind must resolve by IDENTITY, not by predicate TYPE ──────
#
# The two tests below are a matched pair and neither is sufficient alone
# (guard-2319): the first pins that a precondition is not mislabeled `fire_when`,
# the second pins that a real fire_when IS still labeled `fire_when`. An
# implementation that hardcoded "precondition" would pass the first perfectly.


def test_failing_precondition_is_not_mislabeled_when_types_collide(
    monkeypatch, make_sweep_agent
):
    """A precondition and a fire_when of the SAME type: the label must follow WHICH failed.

    MUTATION-VERIFIED against the pre-fix implementation
    (`failed[0].type == fire_when.get("type")`), which yields `fire_when:file_check`
    here and fails this test. The two gates share type `file_check`, so type
    equality cannot tell them apart — and under `mode="fail_fast"` the sweep stops
    at the failing PRECONDITION, so the fire_when is never even evaluated.

    This was cosmetic while gate_kind only fed a stdout line. g-005-28 promoted it
    into `last_shelve_reason`, a DURABLE field on the goal record that guard-2197
    sends readers to — so the mislabel now persists and misdirects whoever reads it
    toward an upstream signal that was never the blocker.
    """
    mod = _load_sweep_module()
    make_sweep_agent([
        _recurring_goal(
            "g-collide",
            preconditions=[{"type": "file_check", "path": _ABSENT, "condition": "exists"}],
            fire_when={"type": "file_check", "path": _PRESENT, "condition": "exists"},
        )
    ])
    _rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    ours = [c for c in calls if c[0] == "g-collide"]
    assert ours, "sweep wrote nothing for the collision goal — assertion would be vacuous"
    reason = {c[1]: c[2] for c in ours}["last_shelve_reason"]
    assert reason.startswith("precondition:"), (
        f"the PRECONDITION failed but the trace says {reason!r} — gate_kind resolved "
        "by predicate type, which cannot distinguish two gates sharing a type"
    )


def test_failing_fire_when_is_still_labeled_fire_when(monkeypatch, make_sweep_agent):
    """The acceptance half: a genuine fire_when failure keeps its own label.

    Without this, a 'fix' that always answered "precondition" would satisfy the
    collision test above while destroying the distinction the field exists to carry.
    """
    mod = _load_sweep_module()
    make_sweep_agent([
        _recurring_goal(
            "g-firewhen-only",
            fire_when={"type": "file_check", "path": _ABSENT, "condition": "exists"},
        )
    ])
    _rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    ours = [c for c in calls if c[0] == "g-firewhen-only"]
    assert ours, "sweep wrote nothing for the fire_when goal — assertion would be vacuous"
    reason = {c[1]: c[2] for c in ours}["last_shelve_reason"]
    assert reason == "fire_when:file_check", reason


def test_trace_is_written_before_the_advance(monkeypatch, sweep_agent):
    """Write ORDER is the fail-safe property, not an implementation detail.

    The three writes are separate subprocess calls with no transaction, so a crash
    can land any PREFIX. Order decides which way a partial write reads under the
    `lastAchievedAt == last_shelved_at => shelved` discriminator:

      trace LAST  — lastAchievedAt fresh, last_shelved_at stale => NOT-EQUAL =>
                    "genuine close". The exact false reading the trace exists to
                    prevent, manufactured by the trace's own ordering. Fail-DANGEROUS.
      trace FIRST — last_shelved_at fresh, lastAchievedAt old => NOT-EQUAL =>
                    "not shelved this cycle", which is TRUE. Fail-SAFE.

    Asserting on the recorded call sequence is the only way to pin this: the final
    state is identical either way, so no end-state assertion can detect a regression.
    """
    mod = _load_sweep_module()
    _rc, calls = _run_sweep_capturing_writes(monkeypatch, mod)
    order = [c[1] for c in calls if c[0] == "g-shelve-me"]
    assert "lastAchievedAt" in order and "last_shelved_at" in order, order
    assert order.index("last_shelved_at") < order.index("lastAchievedAt"), (
        f"trace must be written BEFORE the advance; got {order}"
    )
    assert order.index("last_shelve_reason") < order.index("lastAchievedAt"), order
