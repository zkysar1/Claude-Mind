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
        #  lane-legality limb.
        no_reroute=False,
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


# ---------------------------------------------------------------------------
#  — recurring goals aged on lastAchievedAt, and the two ways that
# fix can silence the very rows this detector exists to surface.
#
# Each test below is falsified by a DISTINCT wrong implementation:
#   age every goal on created_at            -> test_recurring_ages_from_last_achieved_at
#   age every recurring on lastAchievedAt   -> test_shelved_recurring_is_not_silenced
#   treat achievedCount==0 as achieved      -> test_never_achieved_recurring_ages_from_created_at
#   omit the new basis from the tally       -> test_age_basis_breakdown_counts_last_achieved_at
#   exclude every structured defer prefix   -> test_human_blocked_defer_keeps_ageing
#   exclude shelved recurring rows too      -> test_shelved_recurring_survives_the_defer_exclusion
#   hardcode a prefix that later moves      -> test_self_clearing_defer_prefix_still_exists_in_ssot
# ---------------------------------------------------------------------------

def _make_recurring(goal_id, created_hours_ago, achieved_hours_ago=None,
                    achieved_count=5, shelved=False, defer_reason=None,
                    intended_agent="bravo"):
    """A recurring INBOUND goal.

    `shelved` sets last_shelved_at == lastAchievedAt, which is guard-2197's
    single-read test for "the precondition sweep advanced this stamp, no close
    did".
    """
    g = {
        "id": goal_id,
        "title": "Recurring: sensor %s" % goal_id,
        "status": "pending",
        "priority": "MEDIUM",
        "intended_agent": intended_agent,
        "participants": ["agent"],
        "recurring": True,
        "achievedCount": achieved_count,
        "created_at": _iso(created_hours_ago),
    }
    if achieved_hours_ago is not None:
        stamp = _iso(achieved_hours_ago)
        g["lastAchievedAt"] = stamp
        if shelved:
            g["last_shelved_at"] = stamp
    if defer_reason is not None:
        g["defer_reason"] = defer_reason
    return g


def test_recurring_ages_from_last_achieved_at():
    """THE DEFECT. A recurring goal returns to pending on close and never
    leaves, so created_at never advances and its reported age grows without
    bound. Measured 2026-09-06: g-353-02 fired ~6h before the scan and was
    reported at 1174.72h / HIGH on a created_at basis."""
    mod = _import_module()
    g = _make_recurring("g-test-700", created_hours_ago=1174.0,
                        achieved_hours_ago=6.0, achieved_count=23)
    _install_mock_goals(mod, [g])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert ib["matched_count"] == 1, ib
    assert ib["aged_count"] == 0, (
        "a sensor that achieved 6h ago must not be aged at all: %r" % ib)
    age, basis = mod._inbound_age(g, dt.datetime.now())
    assert basis == "lastAchievedAt", basis
    assert age < 7.0, age


def test_shelved_recurring_is_not_silenced():
    """guard-2197: recurring-precondition-sweep.py advances lastAchievedAt on a
    FAILING precondition and never writes achievedCount, so a fresh stamp is not
    evidence of achievement. A shelved sensor is exactly what this detector must
    surface, so it keeps ageing on created_at. Measured live: g-115-105
    (achievedCount 386, shelved, 3258.87h) must stay reported."""
    mod = _import_module()
    g = _make_recurring("g-test-701", created_hours_ago=3258.0,
                        achieved_hours_ago=0.5, achieved_count=386,
                        shelved=True)
    _install_mock_goals(mod, [g])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    age, basis = mod._inbound_age(g, dt.datetime.now())
    assert basis == "created_at", (
        "a SHELVED recurring goal must not age from lastAchievedAt: %r" % basis)
    assert age > 3000.0, age
    assert "g-test-701" in [r["goal_id"] for r in ib["reported"]], ib


def test_never_achieved_recurring_ages_from_created_at():
    """achievedCount == 0 means never achieved, which is genuinely aged since
    birth — the goal's own outcome 1 keeps those on created_at."""
    mod = _import_module()
    g = _make_recurring("g-test-702", created_hours_ago=900.0,
                        achieved_hours_ago=2.0, achieved_count=0)
    _, basis = mod._inbound_age(g, dt.datetime.now())
    assert basis == "created_at", basis


def test_non_recurring_chain_is_unchanged():
    """The recurring branch must not disturb the existing fallback order."""
    mod = _import_module()
    g = _make_inbound("g-test-703", hours_ago=300.0,
                      age_field="handoff_created_at")
    _, basis = mod._inbound_age(g, dt.datetime.now())
    assert basis == "handoff_created_at", basis


def test_age_basis_breakdown_counts_last_achieved_at():
    """The tally hardcodes its bases, so a basis missing from that tuple is
    counted by nothing and the breakdown silently under-reports the population
    it claims to describe."""
    mod = _import_module()
    _install_mock_goals(mod, [
        _make_recurring("g-test-704", created_hours_ago=1000.0,
                        achieved_hours_ago=500.0, achieved_count=9),
    ])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]
    assert ib["age_basis_breakdown"].get("lastAchievedAt") == 1, \
        ib["age_basis_breakdown"]


def test_self_clearing_defer_is_excluded_and_counted():
    """A precondition_unmet: row re-probes on its own cadence, so escalating it
    as un-attended is noise on a shared surface. Excluded — and COUNTED, so the
    exclusion is never silent (guard-1802)."""
    mod = _import_module()
    g = _make_inbound("g-test-705", hours_ago=800.0)
    g["defer_reason"] = "precondition_unmet: waiting on a live env-server"
    _install_mock_goals(mod, [g])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert ib["matched_count"] == 0, ib
    assert ib["excluded_self_clearing_defer"] == 1, ib
    assert "g-test-705" not in [r["goal_id"] for r in ib["reported"]], ib


def test_human_blocked_defer_keeps_ageing():
    """human_blocked: NEVER auto-clears, so ageing it toward a human is exactly
    the intended behaviour. Excluding every structured prefix would bury it."""
    mod = _import_module()
    g = _make_inbound("g-test-706", hours_ago=800.0)
    g["defer_reason"] = "human_blocked: needs an approval click"
    _install_mock_goals(mod, [g])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert ib["excluded_self_clearing_defer"] == 0, ib
    assert "g-test-706" in [r["goal_id"] for r in ib["reported"]], ib


def test_shelved_recurring_survives_the_defer_exclusion():
    """THE INTERACTION BETWEEN THIS GOAL'S TWO HALVES. A shelved recurring goal
    shelves BECAUSE its precondition keeps failing, so it also carries a
    precondition_unmet: defer — and a blanket exclusion drops the very row the
    recurring branch deliberately kept ageing. Measured live 2026-09-06 before
    the carve-out existed: g-115-105 left the reported set."""
    mod = _import_module()
    g = _make_recurring("g-test-707", created_hours_ago=3258.0,
                        achieved_hours_ago=0.5, achieved_count=386,
                        shelved=True,
                        defer_reason="precondition_unmet: gate still failing")
    _install_mock_goals(mod, [g])
    ib = mod.run(_make_args(agent="bravo"))["inbound"]

    assert mod._has_self_clearing_defer(g) is False, \
        "a shelved recurring goal is not attended work on a cadence"
    assert ib["excluded_self_clearing_defer"] == 0, ib
    assert "g-test-707" in [r["goal_id"] for r in ib["reported"]], ib


def test_self_clearing_defer_prefix_still_exists_in_ssot():
    """The predicate writes 'precondition_unmet:' literally, because importing
    the whole set would wrongly exclude human_blocked: too. The cost of the
    literal is that a RENAME leaves it silently matching nothing — so assert
    against the SSOT that owns the prefix list."""
    from gates.defer_classifier import STRUCTURED_DEFER_PREFIXES
    assert "precondition_unmet:" in STRUCTURED_DEFER_PREFIXES, \
        STRUCTURED_DEFER_PREFIXES


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
                test_inbound_absent_when_disabled_and_outbound_keys_intact,
                test_recurring_ages_from_last_achieved_at,
                test_shelved_recurring_is_not_silenced,
                test_never_achieved_recurring_ages_from_created_at,
                test_non_recurring_chain_is_unchanged,
                test_age_basis_breakdown_counts_last_achieved_at,
                test_self_clearing_defer_is_excluded_and_counted,
                test_human_blocked_defer_keeps_ageing,
                test_shelved_recurring_survives_the_defer_exclusion,
                test_self_clearing_defer_prefix_still_exists_in_ssot):
        _fn()
        print("PASS %s" % _fn.__name__)
    print("OK: 24/24 passed")


# ---------------------------------------------------------------------------
# Lane-legality limb ()
# ---------------------------------------------------------------------------
# The sweep's predicate was TIME ONLY, so a handoff addressed to an agent a
# standing lane pin FORBIDS from claiming it aged forever. These cases pin the
# verdict split and, above all, the ASYMMETRIC POSTURE: only a handoff the claim
# gate would actually BLOCK is re-routed; ambiguous / unmatched / unknown are
# left alone, because a false re-route steals a partner's legitimate work while
# a false leave-alone is merely the status quo.
#
# guard-2903 governs how these are written. "The goal is LEFT ALONE" is an
# INVARIANCE assertion and is green by default when broken: a blind comparator,
# an intervention that silently no-opped, and a genuinely-untouched goal all
# produce the same pass. So every leave-alone case below is paired with (a) a
# SENSITIVITY control drawn from the SAME population (a confident out-of-lane
# goal in the same run, which MUST be re-routed — if it is not, the limb no-opped
# and the leave-alone proves nothing), (b) a DETERMINISM control (the same run
# twice, identical), and (c) a POSITIVE assertion that the limb actually RAN
# (registry_readable True and a real verdict per candidate), never inferred from
# the absence of a re-route.

_FIXTURE_REGISTRY = """
## Standing Lane Pins

| id | agent | pinned lane (what the agent DOES) | out of lane (what the agent must NOT select/claim) | granted | source | expires | review_by |
|----|-------|-----------------------------------|---------------------------------------------------|---------|--------|---------|-----------|
| pin-test | foxtrot | RUN ROBLOX WORLDS: no-player sessions, in-session verification, bridge relay fixes | ALL CODE work: client lua, server lua, framework scripts | 2026-08-06 | user directive | user directive only | 2026-11-04 |
"""


def _install_registry(mod, text=_FIXTURE_REGISTRY, world_dir="/fixture/world"):
    """Feed the limb a known registry without touching the real world dir."""
    mod._world_dir = lambda: world_dir
    mod._registry_text = lambda wd: text


def _lane_goal(goal_id, title, hours_ago=200.0, handoff_to="foxtrot"):
    g = _make_handoff(goal_id, hours_ago=hours_ago, handoff_to=handoff_to)
    g["title"] = title
    g["description"] = title
    g["category"] = ""
    return g


# The three population members, by how pin-test sees them.
_OUT = ("g-lane-out", "Fix the client lua handler that drops NPC input")
_BOTH = ("g-lane-both", "Repair the bridge relay and the client lua that calls it")
_NEITHER = ("g-lane-neither", "Wire Emote action to emotional state so NPCs express feelings")


def _run_lane(apply_=True, **overrides):
    mod = _import_module()
    _install_registry(mod, **{k: v for k, v in overrides.items()
                              if k in ("text", "world_dir")})
    _install_mock_goals(mod, [_lane_goal(*_OUT), _lane_goal(*_BOTH),
                              _lane_goal(*_NEITHER)])
    args = _make_args(apply=apply_, agent="alpha", no_board=True,
                      **{k: v for k, v in overrides.items()
                         if k not in ("text", "world_dir")})
    return mod.run(args)


def test_lane_limb_actually_ran_and_classified_every_candidate():
    """Control (c): the intervention TOOK EFFECT, asserted on its own terms.

    Without this, every leave-alone case below is satisfied by a limb that never
    ran at all — the g-115-5226 shape, where a lane verdict of no-pin from an
    unwired call is byte-identical to a real one.
    """
    r = _run_lane()
    split = r["lane_split"]
    assert split["registry_readable"] is True, (
        "the limb did not read a registry, so every verdict below is vacuous: %r" % split)
    assert r["candidate_count"] == 3, r["candidates"]
    for c in r["candidates"]:
        assert c["lane"]["verdict"] != "unknown", (
            "candidate %s got no verdict — the limb degraded silently: %r"
            % (c["goal_id"], c["lane"]))


def test_confident_out_of_lane_is_rerouted():
    """SENSITIVITY control for every leave-alone case: the comparator DOES fire."""
    r = _run_lane()
    ids = [x["goal_id"] for x in r["rerouted"]]
    assert ids == [_OUT[0]], (
        "the one handoff the claim gate would BLOCK must be re-routed; got %r "
        "(if this is empty the limb no-opped and the leave-alone tests below "
        "prove nothing — guard-2903)" % r["rerouted"])
    assert r["lane_split"]["mis_routed"] == [_OUT[0]]
    entry = r["rerouted"][0]
    assert entry["pin_id"] == "pin-test"
    assert entry["evidence"], "a confident block must name its out-of-lane evidence"


def test_ambiguous_and_unmatched_are_left_alone():
    """The asymmetric posture: matched BOTH columns, or NEITHER → do not re-route."""
    r = _run_lane()
    by = {c["goal_id"]: c for c in r["candidates"]}
    assert by[_BOTH[0]]["lane"]["verdict"] == "ambiguous", by[_BOTH[0]]["lane"]
    assert by[_NEITHER[0]]["lane"]["verdict"] == "unmatched", by[_NEITHER[0]]["lane"]
    for gid in (_BOTH[0], _NEITHER[0]):
        assert by[gid]["mis_routed"] is False
        assert gid not in [x["goal_id"] for x in r["rerouted"]], (
            "%s was re-routed on a non-confident verdict — a false re-route "
            "steals a partner's legitimate work" % gid)


def test_unmatched_is_not_reported_as_in_lane():
    """`in-lane` with EMPTY evidence means the pin did not settle the goal.

    lane_pin says "in-lane" there because allow-on-doubt is right at CLAIM time.
    Carrying that word into a ROUTING report would convert "unknown" into
    "legitimate" and hide exactly the population this goal exists to drain.
    """
    r = _run_lane()
    by_verdict = r["lane_split"]["by_verdict"]
    assert _NEITHER[0] in by_verdict.get("unmatched", []), by_verdict
    assert _NEITHER[0] not in by_verdict.get("in-lane", []), (
        "an unmatched goal was reported as in-lane — the silent-pass defect")


def test_unreadable_registry_reroutes_nothing_and_says_so():
    """A degraded run must be visibly degraded, never a clean all-clear."""
    r = _run_lane(text=None)
    assert r["lane_split"]["registry_readable"] is False
    assert r["rerouted"] == [], "nothing may be re-routed without a registry"
    for c in r["candidates"]:
        assert c["lane"]["verdict"] == "unknown", c["lane"]
        assert c["mis_routed"] is False


def test_no_reroute_flag_suppresses_the_write_but_keeps_the_verdict():
    """guard-6571: the escape hatch must not also blind the report."""
    r = _run_lane(no_reroute=True)
    assert r["rerouted"] == []
    assert r["lane_split"]["mis_routed"] == [_OUT[0]], (
        "--no-reroute must still COMPUTE and report the verdict; suppressing "
        "the verdict too would hide the population")


def test_lane_verdicts_are_deterministic():
    """DETERMINISM control (guard-2903 (b)): same input twice, same verdicts.

    Without it a green run is noise-luck and any future red is unattributable.
    """
    a = _run_lane(apply_=False)
    b = _run_lane(apply_=False)
    va = {c["goal_id"]: c["lane"]["verdict"] for c in a["candidates"]}
    vb = {c["goal_id"]: c["lane"]["verdict"] for c in b["candidates"]}
    assert va == vb, (va, vb)
    assert len(set(va.values())) > 1, (
        "all three fixtures resolved to ONE verdict, so the comparator is blind "
        "and the determinism pass is meaningless: %r" % va)


# --------------------------------------------------- empty-registry degrade --

def _write_registry(tmp_path, body):
    """Materialise a world dir whose lane-pin registry holds `body`."""
    from gates import lane_pin as _lp
    p = tmp_path / _lp.REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return tmp_path


def test_empty_registry_degrades_to_unreadable(tmp_path):
    """An EMPTY read is a DEGRADED read, not a registry with no pins.

    The try/except in _registry_text catches an unreadable file, but a file
    that reads as "" sails through it. Handed "" instead of None, lane_pin
    returns verdict="no-pin" -- byte-identical to "this agent has no pin" --
    while _lane_split reports registry_readable TRUE, so a truncated registry
    becomes a confident all-clear declaring every aged handoff legitimate.
    That is the g-115-5226 silent-pass defect one step in. Measured on this
    fleet, not hypothetical: rb-2970 records reads transiently returning
    EMPTY on the S3-backed own-cloud mount while a file settles.
    """
    mod = _import_module()
    for body in ("", "   \n\t\n  "):
        got = mod._registry_text(_write_registry(tmp_path, body))
        assert got is None, "empty registry %r read as readable: %r" % (body, got)


def test_nonempty_registry_still_reads(tmp_path):
    """POSITIVE CONTROL for the guard above.

    A guard that returned None unconditionally would satisfy the test above
    and silently blind the whole limb -- every verdict "unknown", nothing ever
    re-routed, and the sweep reporting itself permanently degraded.
    """
    mod = _import_module()
    body = "# capability-routing\n\npin-001 | foxtrot | out-of-lane: client Lua\n"
    assert mod._registry_text(_write_registry(tmp_path, body)) == body
