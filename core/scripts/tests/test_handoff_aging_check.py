"""test_handoff_aging_check.py — regression tests for .

Asserts that handoff-aging-check.py's scan + cooldown logic correctly:
  1. NOOP when no cross-agent handoff has aged past escalate_hours (the
     typical idle path — a handoff created 1h ago is too fresh; precheck
     phase 0.5b.2b must not post spurious board notes).
  2. FIRES a board escalation when a handoff routed to ANOTHER agent has aged
     past escalate_hours AND no prior cooldown entry exists (the canonical
     incident this phase exists to catch — fresh-eyes-review 2026-06-18 found
     6 handoffs aged 78-782h with an EMPTY escalation log).
  3. NOOPS when a recent `handoff-aged` board post for this goal_id exists
     within the window — posted by ANY agent (g-115-1531: the shared, durable
     board-scan cooldown that replaced the per-agent WM proactive_escalation_log,
     which spawned ~30 duplicate posts from 6 agents on 2026-06-18).
  4. SKIPS a handoff routed to SELF (handoff_to == self_agent) — only goals
     routed elsewhere are the partner's missed work.
  5. SKIPS a handoff with no handoff_created_at (cannot compute age).
  6. FIRES again when the only board post is OLDER than the window (re-escalation).
  7. Does NOT suppress when the recent board post is for a DIFFERENT goal_id.

Pattern mirrors test_inbox_alert_age_check.py: importlib load +
monkeypatch on _read_goals so the suite never hits the daemon. Uses
--board-escalation-log <tmp.json> (a JSON list of coordination-board posts
standing in for the live board scan) + --no-board so the test exercises the
apply path without spawning board-read.sh / board-post.sh.

Closes acceptance criterion "Tests: 3 cases" from g-115-1524 (7 provided);
g-115-1531 added the cross-agent dedup + window-boundary + goal-specificity cases.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))


def _import_module():
    """Load handoff-aging-check.py via importlib (hyphen-free attribute name)."""
    spec = importlib.util.spec_from_file_location(
        "handoff_aging_check_mod",
        CORE_SCRIPTS / "handoff-aging-check.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for handoff-aging-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(hours_ago: float) -> str:
    t = dt.datetime.now() - dt.timedelta(hours=hours_ago)
    return t.isoformat(timespec="seconds")


def _make_handoff(goal_id: str, hours_ago, handoff_to: str = "alpha",
                  status: str = "pending", with_created: bool = True) -> dict:
    """Synthesize a cross-agent handoff goal record."""
    g = {
        "id": goal_id,
        "title": "Apply: cross-agent work for %s" % goal_id,
        "status": status,
        "handoff_to": handoff_to,
        "handoff_from": "bravo",
        "participants": ["agent"],
    }
    if with_created and hours_ago is not None:
        g["handoff_created_at"] = _iso(hours_ago)
    return g


def _make_args(**overrides):
    """Build a Namespace matching argparse output."""
    defaults = dict(
        apply=False,
        escalate_hours=72.0,
        agent="bravo",
        board_escalation_log=None,
        no_board=True,
        #  inbound pass. Present here so this helper keeps matching
        # argparse output as the docstring claims; run() also reads both via
        # getattr defaults, so a caller omitting them still works.
        inbound_max_report=None,
        no_inbound=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _install_mock_goals(mod, world_goals: list, agent_goals: list = None):
    """Monkeypatch _read_goals to return synthetic world/agent queues."""
    agent_goals = agent_goals or []
    mod._read_goals = lambda source: list(world_goals) if source == "world" else list(agent_goals)


def test_no_aged_handoff_noop():
    """Case 1: handoff created 1h ago — below escalate threshold (72h) → noop."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_handoff("g-test-001", hours_ago=1.0)])
    args = _make_args(apply=True)
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["scanned"] == 1
    assert result["candidate_count"] == 0, (
        "fresh handoff (1h) must not be a candidate — under escalate threshold (72h). "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0
    assert result["fired"] == []
    assert result["failed"] == []


def test_aged_handoff_fires(tmp_path):
    """Case 2: handoff to alpha aged 100h (>= 72h, empty board) → fires."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_handoff("g-test-002", hours_ago=100.0, handoff_to="alpha")])

    board_path = tmp_path / "board.json"
    board_path.write_text("[]", encoding="utf-8")  # no prior escalation on the board

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["scanned"] == 1
    assert result["candidate_count"] == 1, (
        "100h-aged handoff must be a candidate (>= 72h). candidates=%r"
        % result["candidates"])
    cand = result["candidates"][0]
    assert cand["handoff_to"] == "alpha"
    assert cand["on_cooldown"] is False, "empty board → no cooldown"
    assert result["applied"] == 1, "aged handoff with no prior board post must fire"
    assert len(result["fired"]) == 1
    fired = result["fired"][0]
    assert fired["goal_id"] == "g-test-002"
    assert fired["handoff_to"] == "alpha"
    assert fired["detail"] == "no_board", (
        "test mode flag --no-board should short-circuit board-post.sh and return 'no_board'")


def test_cross_agent_board_cooldown_noop(tmp_path):
    """Case 3 ( core fix): a recent `handoff-aged` board post for this
    goal_id — posted by a DIFFERENT agent (charlie) 1h ago — suppresses self's
    (bravo's) escalation. This is the shared, durable cooldown: the per-agent WM
    log is gone; one team-wide board post per window is the cooldown."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_handoff("g-test-003", hours_ago=100.0, handoff_to="delta")])

    board_path = tmp_path / "board.json"
    # A post by charlie (NOT self=bravo) for the same goal_id, 1h ago (< 72h window).
    board_path.write_text(json.dumps([{
        "author": "charlie",
        "type": "status",
        "tags": ["handoff-aged", "g-test-003", "delta"],
        "timestamp": _iso(hours_ago=1.0),
    }]), encoding="utf-8")

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["candidate_count"] == 1, "still a candidate (just on cooldown)"
    cand = result["candidates"][0]
    assert cand["on_cooldown"] is True, (
        "expected on_cooldown=True: a 1h-old CROSS-AGENT board post (< 72h) must "
        "suppress re-escalation. candidate=%r" % cand)
    assert result["applied"] == 0, "shared board cooldown must suppress the fire"
    assert result["fired"] == []
    assert result["skipped_cooldown"] == ["g-test-003"]
    # Board file unchanged — a cooldown skip never re-posts.
    assert json.loads(board_path.read_text(encoding="utf-8"))[0]["author"] == "charlie"


def test_board_post_outside_window_fires(tmp_path):
    """Case 6 (): a `handoff-aged` board post OLDER than escalate_hours
    does NOT suppress — the window elapsed, so the handoff re-escalates."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_handoff("g-test-006", hours_ago=200.0, handoff_to="echo")])

    board_path = tmp_path / "board.json"
    # A post 100h ago — OLDER than the 72h escalate window → not a cooldown.
    board_path.write_text(json.dumps([{
        "author": "zeta",
        "type": "status",
        "tags": ["handoff-aged", "g-test-006", "echo"],
        "timestamp": _iso(hours_ago=100.0),
    }]), encoding="utf-8")

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["on_cooldown"] is False, (
        "a 100h-old board post is outside the 72h window — must not suppress")
    assert result["applied"] == 1, "handoff re-escalates after the cooldown window elapses"


def test_board_post_other_goal_does_not_suppress(tmp_path):
    """Case 7 (): a recent `handoff-aged` post for a DIFFERENT goal_id
    must NOT suppress this goal — the cooldown is keyed per goal_id tag."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_handoff("g-test-007", hours_ago=100.0, handoff_to="alpha")])

    board_path = tmp_path / "board.json"
    board_path.write_text(json.dumps([{
        "author": "charlie",
        "type": "status",
        "tags": ["handoff-aged", "g-OTHER-999", "alpha"],
        "timestamp": _iso(hours_ago=1.0),
    }]), encoding="utf-8")

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["candidates"][0]["on_cooldown"] is False, (
        "a recent post for g-OTHER-999 must not suppress g-test-007")
    assert result["applied"] == 1


def test_self_routed_skipped():
    """Case 4: handoff routed to SELF (bravo) — must not be a candidate."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_handoff("g-test-004", hours_ago=200.0, handoff_to="bravo")])
    args = _make_args(apply=True, agent="bravo")
    result = mod.run(args)

    assert result["candidate_count"] == 0, (
        "handoff routed to self (bravo) must be skipped — only partner-routed handoffs escalate. "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0


def test_missing_created_at_skipped():
    """Case 5: handoff_to set but no handoff_created_at — cannot age, skip."""
    mod = _import_module()
    _install_mock_goals(
        mod, [_make_handoff("g-test-005", hours_ago=None, handoff_to="echo", with_created=False)])
    args = _make_args(apply=True)
    result = mod.run(args)

    assert result["candidate_count"] == 0, (
        "handoff with no handoff_created_at must be skipped (no age basis). "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0


# ─────────────────────── inbound pass () ───────────────────────
# The outbound cases above cover work routed AWAY from self. These cover the
# mirror — work routed TO self, which nothing aged before , so the
# one queue an agent must DRAIN was the one queue with no aging sweep.


def _make_inbound(goal_id: str, hours_ago, intended_agent="bravo",
                  priority="MEDIUM", status="pending",
                  age_field="created_at", handoff_to=None) -> dict:
    """Synthesize an INBOUND goal (routed to self via intended_agent/handoff_to)."""
    g = {
        "id": goal_id,
        "title": "Inbound work %s" % goal_id,
        "status": status,
        "priority": priority,
        "intended_agent": intended_agent,
        "participants": ["agent"],
    }
    if handoff_to is not None:
        g["handoff_to"] = handoff_to
    if hours_ago is not None and age_field:
        g[age_field] = _iso(hours_ago)
    return g


def test_inbound_positive_control_aged_goal_is_named():
    """POSITIVE CONTROL (the goal's explicit VERIFY requirement): an aged
    inbound goal must be NAMED, not merely counted. A sweep reporting 0 over
    an empty predicate is indistinguishable from a clean queue (guard-1715 /
    rb-245), so the assertion is on the goal_id appearing in `reported`."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_inbound("g-test-100", hours_ago=500.0)])
    result = mod.run(_make_args(agent="bravo"))

    ib = result["inbound"]
    assert ib["matched_count"] == 1, ib
    assert ib["aged_count"] == 1, ib
    ids = [r["goal_id"] for r in ib["reported"]]
    assert "g-test-100" in ids, "aged inbound goal must be NAMED, got %r" % ids


def test_inbound_created_at_fallback_is_load_bearing():
    """The fallback is not polish. Measured live 2026-08-11: only 2 of 196
    inbound goals carried handoff_created_at while 196 carried created_at, so a
    pass aged solely on handoff_created_at reports a 2-of-196 view that looks
    like a nearly-clean queue. Pin that a created_at-only goal is still aged,
    AND that the basis is reported so the age is not mistaken for routing age."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_inbound("g-test-101", hours_ago=300.0,
                                            age_field="created_at")])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert ib["aged_count"] == 1, ib
    row = ib["reported"][0]
    assert row["age_basis"] == "created_at", row
    assert ib["age_basis_breakdown"]["created_at"] == 1, ib["age_basis_breakdown"]


def test_inbound_handoff_to_self_is_caught():
    """The outbound pass SKIPS handoff_to == self by construction (only
    partner-routed handoffs escalate there), so without this predicate those
    goals are aged by nothing at all. Live count on cc-08 was 3, one of which
    intended_agent did not also cover."""
    mod = _import_module()
    g = _make_inbound("g-test-102", hours_ago=400.0,
                      intended_agent="either", handoff_to="bravo")
    _install_mock_goals(mod, [g])
    result = mod.run(_make_args(agent="bravo"))

    assert result["candidate_count"] == 0, "still not an OUTBOUND candidate"
    ib = result["inbound"]
    assert ib["aged_count"] == 1, ib
    assert ib["reported"][0]["routed_by"] == "handoff_to", ib["reported"][0]


def test_inbound_either_is_not_inbound():
    """'either' means UNROUTED and is the dominant value (898 of 1520 pending
    live). Treating it as inbound would swallow ~59% of the queue and make the
    pass meaningless."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_inbound("g-test-103", hours_ago=900.0,
                                            intended_agent="either")])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert ib["matched_count"] == 0, "intended_agent='either' must not count as inbound"
    assert ib["reported"] == []


def test_inbound_fresh_goal_not_reported():
    """Below-threshold inbound work must not be escalated (the idle path)."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_inbound("g-test-104", hours_ago=1.0)])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert ib["matched_count"] == 1, "matched (it IS inbound)"
    assert ib["aged_count"] == 0, "but not aged past escalate_hours"
    assert ib["reported"] == []


def test_inbound_high_is_never_truncated_by_the_cap():
    """The output bound exists so a ~200-row backlog is not emitted whole. It
    must NEVER drop a HIGH row: silently truncating a HIGH goal reproduces the
    exact failure this pass was built to fix (a HIGH directive sat pending
    through four cycles, 2026-08-11)."""
    mod = _import_module()
    goals = [_make_inbound("g-test-high", hours_ago=500.0, priority="HIGH")]
    goals += [_make_inbound("g-test-med-%d" % i, hours_ago=400.0 + i) for i in range(6)]
    _install_mock_goals(mod, goals)
    ib = mod.run(_make_args(agent="bravo", inbound_max_report=0))["inbound"]

    ids = [r["goal_id"] for r in ib["reported"]]
    assert ids == ["g-test-high"], (
        "cap=0 must still report the HIGH row and nothing else, got %r" % ids)
    assert ib["high_count"] == 1
    assert ib["suppressed_count"] == 6, ib


def test_inbound_cap_bounds_non_high_and_reports_suppression():
    """A bounded view must never read as the whole queue — the suppressed
    count is what keeps the bound honest."""
    mod = _import_module()
    goals = [_make_inbound("g-test-m%d" % i, hours_ago=400.0 + i) for i in range(9)]
    _install_mock_goals(mod, goals)
    ib = mod.run(_make_args(agent="bravo", inbound_max_report=3))["inbound"]

    assert ib["aged_count"] == 9, ib
    assert len(ib["reported"]) == 3, ib["reported"]
    assert ib["suppressed_count"] == 6, ib
    # oldest-first ordering: the 3 reported are the 3 largest ages
    ages = [r["age_hours"] for r in ib["reported"]]
    assert ages == sorted(ages, reverse=True), ages
    assert min(ages) >= 406.0, ages


def test_inbound_absent_when_disabled_and_outbound_keys_intact():
    """--no-inbound is the escape hatch; the outbound contract is unchanged
    either way (every pre-existing key still present with its old meaning)."""
    mod = _import_module()
    _install_mock_goals(mod, [_make_inbound("g-test-105", hours_ago=500.0)])
    result = mod.run(_make_args(agent="bravo", no_inbound=True))

    assert "inbound" not in result
    for k in ("mode", "self_agent", "escalate_hours", "scanned", "candidates",
              "candidate_count", "applied", "fired", "skipped_cooldown", "failed"):
        assert k in result, "outbound key %r must survive the inbound addition" % k


if __name__ == "__main__":
    import tempfile
    test_no_aged_handoff_noop()
    print("PASS test_no_aged_handoff_noop")
    with tempfile.TemporaryDirectory() as td:
        test_aged_handoff_fires(Path(td))
    print("PASS test_aged_handoff_fires")
    with tempfile.TemporaryDirectory() as td:
        test_cross_agent_board_cooldown_noop(Path(td))
    print("PASS test_cross_agent_board_cooldown_noop")
    test_self_routed_skipped()
    print("PASS test_self_routed_skipped")
    test_missing_created_at_skipped()
    print("PASS test_missing_created_at_skipped")
    with tempfile.TemporaryDirectory() as td:
        test_board_post_outside_window_fires(Path(td))
    print("PASS test_board_post_outside_window_fires")
    with tempfile.TemporaryDirectory() as td:
        test_board_post_other_goal_does_not_suppress(Path(td))
    print("PASS test_board_post_other_goal_does_not_suppress")
    # inbound pass ()
    for _fn in (test_inbound_positive_control_aged_goal_is_named,
                test_inbound_created_at_fallback_is_load_bearing,
                test_inbound_handoff_to_self_is_caught,
                test_inbound_either_is_not_inbound,
                test_inbound_fresh_goal_not_reported,
                test_inbound_high_is_never_truncated_by_the_cap,
                test_inbound_cap_bounds_non_high_and_reports_suppression,
                test_inbound_absent_when_disabled_and_outbound_keys_intact):
        _fn()
        print("PASS %s" % _fn.__name__)
    print("OK: 15/15 passed")
