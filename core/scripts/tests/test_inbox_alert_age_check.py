"""test_inbox_alert_age_check.py - regression tests for  + .

Asserts that inbox-alert-age-check.py's scan + cooldown logic correctly:
  1. NOOP when no goal in asp-115 has aged past the medium threshold
     (the typical idle path - alert-sweep filed an Unblock 30 minutes ago,
     too fresh to escalate; precheck phase 0.5b.1b must not fire spurious
     notifications).
  2. FIRES a high-severity escalation when an Unblock with
     origin_signal=alert-email:* has aged past the HIGH threshold AND no
     prior board breadcrumb exists (the canonical incident this phase exists
     to catch - finding 2 of g-115-822).
  3. NOOPS when a recent `inbox-alert-aged` board breadcrumb for this goal_id
     exists within the cooldown window - posted by ANY agent (g-115-1533: the
     shared, durable board-scan cooldown that replaced the per-agent WM
     proactive_escalation_log, which would have spawned N duplicate USER EMAILS
     once N agents ran the now-enforced gate - the email-side sibling of the
     g-115-1531 handoff-aging fix).
  4. FIRES again when the only board breadcrumb is OLDER than the severity's
     cooldown window (re-escalation after the window elapses).
  5. Does NOT suppress when the recent breadcrumb is for a DIFFERENT goal_id.
  6. SKIPS a goal whose origin_signal is not `alert-email:*` (not alert-derived).
  7. SKIPS a goal whose title does not start with "Unblock".

Pattern mirrors test_handoff_aging_check.py: importlib load +
monkeypatch on _read_aspiration so the suite never hits the daemon. Uses
--board-escalation-log <tmp.json> (a JSON list of coordination-board posts
standing in for the live board scan) + --no-email + --no-board so the test
exercises the apply path without spawning email-send.sh / board-post.sh.

Closes acceptance criterion "Tests: 3 cases" from g-115-848 (7 provided);
g-115-1533 swapped the per-agent-WM cooldown case for the cross-agent
board-scan dedup + window-boundary + goal-specificity cases.
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
    """Load inbox-alert-age-check.py via importlib (hyphen-free attribute name)."""
    spec = importlib.util.spec_from_file_location(
        "inbox_alert_age_check_mod",
        CORE_SCRIPTS / "inbox-alert-age-check.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load spec for inbox-alert-age-check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(hours_ago: float) -> str:
    t = dt.datetime.now() - dt.timedelta(hours=hours_ago)
    return t.isoformat(timespec="seconds")


def _make_unblock(goal_id: str, hours_ago: float, status: str = "pending",
                  origin: str = None, title: str = None) -> dict:
    """Synthesize an alert-sweep-filed Unblock goal record.

    `origin`/`title` override the alert-email origin_signal / Unblock title so
    the candidate-filter cases (6, 7) can exercise the negative paths.
    """
    return {
        "id": goal_id,
        "title": title if title is not None else (
            "Unblock: alert-sweep finding for s3-key-%s" % goal_id),
        "description": "Subject: Test alert %s\n\nFiled by alert-sweep.sh" % goal_id,
        "status": status,
        "origin_signal": origin if origin is not None else (
            "alert-email:s3-key-%s" % goal_id),
        "created_at": _iso(hours_ago),
        "participants": ["agent"],
    }


def _make_args(**overrides):
    """Build a Namespace matching argparse output."""
    defaults = dict(
        apply=False,
        asp_id="asp-115",
        high_hours=4,
        medium_hours=12,
        board_escalation_log=None,
        no_email=True,
        no_board=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _install_mock_aspirations(mod, goals: list):
    """Monkeypatch _read_aspiration to return a synthetic asp-115."""
    mod._read_aspiration = lambda asp_id: {"id": asp_id, "goals": goals}


def _board_post(goal_id: str, severity: str, hours_ago: float, author: str = "charlie") -> dict:
    """Synthesize a coordination-board `inbox-alert-aged` breadcrumb post."""
    return {
        "author": author,
        "type": "status",
        "tags": ["inbox-alert-aged", goal_id, "severity:%s" % severity],
        "timestamp": _iso(hours_ago=hours_ago),
    }


def test_no_aged_alert_noop():
    """Case 1: alert filed 30 min ago - below both thresholds -> noop."""
    mod = _import_module()
    goals = [_make_unblock("g-test-001", hours_ago=0.5)]
    _install_mock_aspirations(mod, goals)
    args = _make_args(apply=True, board_escalation_log=None)
    result = mod.run(args)

    assert result["mode"] == "apply", "expected apply mode (we passed --apply)"
    assert result["scanned"] == 1, "scanned should count the synthetic goal"
    assert result["candidate_count"] == 0, (
        "fresh alert (0.5h) must not appear in candidates - under medium threshold (12h). "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0, "no escalations should fire"
    assert result["fired"] == []
    assert result["failed"] == []


def test_aged_high_alert_fires(tmp_path):
    """Case 2: alert filed 14h ago (>= high 12h, empty board) -> fires HIGH.

    g-115-1539: HIGH is the LONGER-aged band (>= max(high,medium)=12h), not
    >= high(4h). Aged to 14h so the corrected classifier returns "high".
    """
    mod = _import_module()
    goals = [_make_unblock("g-test-002", hours_ago=14.0)]
    _install_mock_aspirations(mod, goals)

    board_path = tmp_path / "board.json"
    board_path.write_text("[]", encoding="utf-8")  # no prior breadcrumb on the board

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["scanned"] == 1
    assert result["candidate_count"] == 1, (
        "14h-aged alert must be a candidate (>= high 12h). candidates=%r"
        % result["candidates"])
    cand = result["candidates"][0]
    assert cand["severity"] == "high", "expected severity=high, got %r" % cand["severity"]
    assert cand["on_cooldown"] is False, "empty board -> no cooldown"
    assert result["applied"] == 1, "high-severity candidate without cooldown must fire"
    assert len(result["fired"]) == 1
    fired = result["fired"][0]
    assert fired["goal_id"] == "g-test-002"
    assert fired["severity"] == "high"
    assert fired["detail"] == "no_email", (
        "test mode flag --no-email should short-circuit email-send.sh and return 'no_email'")


def test_cross_agent_board_cooldown_noop(tmp_path):
    """Case 3 ( core fix): a recent `inbox-alert-aged` board breadcrumb
    for this goal_id - posted by a DIFFERENT agent (charlie) 1h ago - suppresses
    self's (alpha's) email escalation. This is the shared, durable cooldown: the
    per-agent WM log is gone; one team-wide board breadcrumb per window is the
    cooldown."""
    mod = _import_module()
    goals = [_make_unblock("g-test-003", hours_ago=14.0)]  # >= 12h -> HIGH ()
    _install_mock_aspirations(mod, goals)

    board_path = tmp_path / "board.json"
    # A breadcrumb by charlie (NOT self) for the same goal_id, 1h ago (< 4h HIGH window).
    board_path.write_text(json.dumps([
        _board_post("g-test-003", "high", hours_ago=1.0, author="charlie"),
    ]), encoding="utf-8")

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["candidate_count"] == 1, "still a candidate (just on cooldown)"
    cand = result["candidates"][0]
    assert cand["severity"] == "high"
    assert cand["on_cooldown"] is True, (
        "expected on_cooldown=True: a 1h-old CROSS-AGENT breadcrumb (< 4h HIGH "
        "window) must suppress re-escalation. candidate=%r" % cand)
    assert result["applied"] == 0, "shared board cooldown must suppress the fire"
    assert result["fired"] == []
    assert result["skipped_cooldown"] == ["g-test-003"]
    # Board file unchanged - a cooldown skip never re-posts.
    assert json.loads(board_path.read_text(encoding="utf-8"))[0]["author"] == "charlie"


def test_board_post_outside_window_fires(tmp_path):
    """Case 4 (): an `inbox-alert-aged` breadcrumb OLDER than the
    severity's cooldown window does NOT suppress - the window elapsed, so the
    alert re-escalates. A 5h-old breadcrumb is past the 4h HIGH window."""
    mod = _import_module()
    goals = [_make_unblock("g-test-004", hours_ago=14.0)]  # 14h -> HIGH (>= 12h, )
    _install_mock_aspirations(mod, goals)

    board_path = tmp_path / "board.json"
    # A breadcrumb 5h ago - inside the ~13h scan horizon but past the 4h HIGH window.
    board_path.write_text(json.dumps([
        _board_post("g-test-004", "high", hours_ago=5.0, author="zeta"),
    ]), encoding="utf-8")

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["candidate_count"] == 1
    assert result["candidates"][0]["on_cooldown"] is False, (
        "a 5h-old breadcrumb is past the 4h HIGH cooldown window - must not suppress")
    assert result["applied"] == 1, "alert re-escalates after the cooldown window elapses"


def test_board_post_other_goal_does_not_suppress(tmp_path):
    """Case 5 (): a recent `inbox-alert-aged` breadcrumb for a DIFFERENT
    goal_id must NOT suppress this goal - the cooldown is keyed per goal_id tag."""
    mod = _import_module()
    goals = [_make_unblock("g-test-005", hours_ago=5.0)]
    _install_mock_aspirations(mod, goals)

    board_path = tmp_path / "board.json"
    board_path.write_text(json.dumps([
        _board_post("g-OTHER-999", "high", hours_ago=1.0, author="charlie"),
    ]), encoding="utf-8")

    args = _make_args(apply=True, board_escalation_log=str(board_path))
    result = mod.run(args)

    assert result["candidates"][0]["severity"] == "medium", (
        "g-test-005 aged 5h -> medium band (4-12h) under the g-115-1539 fix")
    assert result["candidates"][0]["on_cooldown"] is False, (
        "a recent breadcrumb for g-OTHER-999 must not suppress g-test-005")
    assert result["applied"] == 1


def test_non_alert_origin_skipped():
    """Case 6: an aged Unblock whose origin_signal is NOT alert-email:* is not
    alert-derived - must not be a candidate."""
    mod = _import_module()
    goals = [_make_unblock("g-test-006", hours_ago=10.0, origin="idea:some-other-source")]
    _install_mock_aspirations(mod, goals)
    args = _make_args(apply=True)
    result = mod.run(args)

    assert result["candidate_count"] == 0, (
        "an Unblock without an alert-email origin_signal must be skipped. "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0


def test_non_unblock_title_skipped():
    """Case 7: an aged alert-email goal whose title does not start with "Unblock"
    (e.g. an Investigate filed from the same alert) is not the escalation target."""
    mod = _import_module()
    goals = [_make_unblock("g-test-007", hours_ago=10.0,
                           title="Investigate: alert-sweep finding for s3-key-007")]
    _install_mock_aspirations(mod, goals)
    args = _make_args(apply=True)
    result = mod.run(args)

    assert result["candidate_count"] == 0, (
        "a non-Unblock title must be skipped (escalation targets Unblock goals). "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0


def test_classify_severity_bands():
    """: _classify_severity maps the LONGER-aged alert to "high".

    Regression for the dead-medium-branch bug: the prior code checked
    `age >= thresholds["high"]` (4h) FIRST, so with high(4) < medium(12) the
    medium branch (age >= 12) was unreachable and EVERY aged alert (>=4h)
    classified "high". The fix uses max()/min() so the medium band (4-12h) is
    reachable and the more-urgent "high" is the longer-aged alert.
    """
    mod = _import_module()
    th = {"high": 4.0, "medium": 12.0}   # canonical defaults: high < medium

    # Under both thresholds -> no escalation.
    assert mod._classify_severity(0.0, th) == ""
    assert mod._classify_severity(3.9, th) == ""
    assert mod._classify_severity(None, th) == ""

    # Aged past the SHORTER interval (4h) but not the longer -> "medium".
    # THE regression assertion: pre-fix these returned "high" (dead medium branch).
    assert mod._classify_severity(4.0, th) == "medium"
    assert mod._classify_severity(6.0, th) == "medium"
    assert mod._classify_severity(11.9, th) == "medium"

    # Aged past the LONGER interval (12h) -> "high" (most urgent).
    assert mod._classify_severity(12.0, th) == "high"
    assert mod._classify_severity(48.0, th) == "high"

    # Config-order robustness: max()/min() keeps older->high even when the keys
    # are swapped so "high" holds the larger value.
    th_swapped = {"high": 12.0, "medium": 4.0}
    assert mod._classify_severity(2.0, th_swapped) == ""
    assert mod._classify_severity(6.0, th_swapped) == "medium"
    assert mod._classify_severity(20.0, th_swapped) == "high"


if __name__ == "__main__":
    import tempfile
    test_no_aged_alert_noop()
    print("PASS test_no_aged_alert_noop")
    with tempfile.TemporaryDirectory() as td:
        test_aged_high_alert_fires(Path(td))
    print("PASS test_aged_high_alert_fires")
    with tempfile.TemporaryDirectory() as td:
        test_cross_agent_board_cooldown_noop(Path(td))
    print("PASS test_cross_agent_board_cooldown_noop")
    with tempfile.TemporaryDirectory() as td:
        test_board_post_outside_window_fires(Path(td))
    print("PASS test_board_post_outside_window_fires")
    with tempfile.TemporaryDirectory() as td:
        test_board_post_other_goal_does_not_suppress(Path(td))
    print("PASS test_board_post_other_goal_does_not_suppress")
    test_non_alert_origin_skipped()
    print("PASS test_non_alert_origin_skipped")
    test_non_unblock_title_skipped()
    print("PASS test_non_unblock_title_skipped")
    test_classify_severity_bands()
    print("PASS test_classify_severity_bands")
    print("OK: 8/8 passed")
