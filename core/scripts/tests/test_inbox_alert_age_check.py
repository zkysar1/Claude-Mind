"""test_inbox_alert_age_check.py — regression tests for .

Asserts that inbox-alert-age-check.py's scan + cooldown logic correctly:
  1. NOOP when no goal in asp-115 has aged past the medium threshold
     (the typical idle path — alert-sweep filed an Unblock 30 minutes ago,
     too fresh to escalate; precheck phase 0.5b.1b must not fire spurious
     notifications).
  2. FIRES a high-severity escalation when an Unblock with
     origin_signal=alert-email:* has aged past the HIGH threshold AND no
     prior cooldown entry exists in proactive_escalation_log (the canonical
     incident this phase exists to catch — finding 2 of g-115-822).
  3. NOOPS when a prior escalation entry is within cooldown window, even
     for an aged goal (caller-side cooldown prevents notification storms).

Pattern: importlib load (matches test_defer_recheck_patterns.py) +
monkeypatch on _rt.aspirations_read/_rt.wm_read so the suite never hits the
daemon. Uses --proactive-escalation-log <tmp.json> + --no-email so the
test exercises the apply path without spawning email-send.sh.

Closes acceptance criterion "Tests: 3 cases" from g-115-848.
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


def _make_unblock(goal_id: str, hours_ago: float, status: str = "pending") -> dict:
    """Synthesize an alert-sweep-filed Unblock goal record."""
    return {
        "id": goal_id,
        "title": "Unblock: alert-sweep finding for s3-key-%s" % goal_id,
        "description": "Subject: Test alert %s\n\nFiled by alert-sweep.sh" % goal_id,
        "status": status,
        "origin_signal": "alert-email:s3-key-%s" % goal_id,
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
        proactive_escalation_log=None,
        no_email=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _install_mock_aspirations(mod, goals: list):
    """Monkeypatch _read_aspiration to return a synthetic asp-115."""
    mod._read_aspiration = lambda asp_id: {"id": asp_id, "goals": goals}


def test_no_aged_alert_noop():
    """Case 1: alert filed 30 min ago — below both thresholds → noop."""
    mod = _import_module()
    goals = [_make_unblock("g-test-001", hours_ago=0.5)]
    _install_mock_aspirations(mod, goals)
    args = _make_args(apply=True, proactive_escalation_log=None)
    result = mod.run(args)

    assert result["mode"] == "apply", "expected apply mode (we passed --apply)"
    assert result["scanned"] == 1, "scanned should count the synthetic goal"
    assert result["candidate_count"] == 0, (
        "fresh alert (0.5h) must not appear in candidates — under medium threshold (12h). "
        "candidates=%r" % result["candidates"])
    assert result["applied"] == 0, "no escalations should fire"
    assert result["fired"] == []
    assert result["failed"] == []


def test_aged_high_alert_fires(tmp_path):
    """Case 2: alert filed 5h ago (>= high 4h, no prior cooldown) → fires HIGH."""
    mod = _import_module()
    goals = [_make_unblock("g-test-002", hours_ago=5.0)]
    _install_mock_aspirations(mod, goals)

    log_path = tmp_path / "proactive_escalation_log.json"
    log_path.write_text("[]", encoding="utf-8")

    args = _make_args(apply=True, proactive_escalation_log=str(log_path))
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["scanned"] == 1
    assert result["candidate_count"] == 1, (
        "5h-aged alert must be a candidate (>= high 4h). candidates=%r"
        % result["candidates"])
    cand = result["candidates"][0]
    assert cand["severity"] == "high", "expected severity=high, got %r" % cand["severity"]
    assert cand["on_cooldown"] is False, "no prior log entry → no cooldown"
    assert result["applied"] == 1, "high-severity candidate without cooldown must fire"
    assert len(result["fired"]) == 1
    fired = result["fired"][0]
    assert fired["goal_id"] == "g-test-002"
    assert fired["severity"] == "high"
    assert fired["detail"] == "no_email", (
        "test mode flag --no-email should short-circuit email-send.sh and return 'no_email'")
    # Cooldown log must now contain the entry (caller-side cooldown contract).
    log_entries = json.loads(log_path.read_text(encoding="utf-8"))
    assert len(log_entries) == 1, "expected one cooldown entry written"
    assert log_entries[0]["blocker_id"] == "inbox_alert_g-test-002"
    assert log_entries[0]["severity"] == "high"


def test_cooldown_active_noop(tmp_path):
    """Case 3: 5h-aged alert WITH a recent cooldown entry (1h ago) → on_cooldown → no fire."""
    mod = _import_module()
    goals = [_make_unblock("g-test-003", hours_ago=5.0)]
    _install_mock_aspirations(mod, goals)

    # Cooldown threshold for HIGH is 4h. A log entry 1h ago means the next
    # fire must wait 3 more hours. The sweep should observe on_cooldown=True
    # and skip the notification.
    log_path = tmp_path / "proactive_escalation_log.json"
    recent_entry = [{
        "blocker_id": "inbox_alert_g-test-003",
        "severity": "high",
        "sent_at": _iso(hours_ago=1.0),
    }]
    log_path.write_text(json.dumps(recent_entry), encoding="utf-8")

    args = _make_args(apply=True, proactive_escalation_log=str(log_path))
    result = mod.run(args)

    assert result["mode"] == "apply"
    assert result["candidate_count"] == 1, "still a candidate (just on cooldown)"
    cand = result["candidates"][0]
    assert cand["severity"] == "high"
    assert cand["on_cooldown"] is True, (
        "expected on_cooldown=True given 1h-old log entry < high threshold 4h. "
        "last=%r" % cand["last_escalation"])
    assert result["applied"] == 0, "cooldown must suppress the fire"
    assert result["fired"] == []
    assert result["skipped_cooldown"] == ["g-test-003"]
    # Log entries unchanged — cooldown skip MUST NOT write a new entry
    # (otherwise the cooldown extends indefinitely).
    log_after = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_after == recent_entry, "cooldown skip must not append to the log"


if __name__ == "__main__":
    # Allow running standalone without pytest (matches test_cross_repo_commit.py pattern).
    import tempfile
    test_no_aged_alert_noop()
    print("PASS test_no_aged_alert_noop")
    with tempfile.TemporaryDirectory() as td:
        test_aged_high_alert_fires(Path(td))
    print("PASS test_aged_high_alert_fires")
    with tempfile.TemporaryDirectory() as td:
        test_cooldown_active_noop(Path(td))
    print("PASS test_cooldown_active_noop")
    print("OK: 3/3 passed")
