"""test_quiescence_prolonged_ping.py — prolonged-quiescence escalation ().

Exercises _evaluate_prolonged_quiescence in quiescence-gate.py: the helper that
decides whether a long, all-user-gated quiescence warrants ONE focused
escalating user-ping, and throttles it per blocker-set hash so a long window
produces at most one notification.

Fires only when BOTH hold:
  (a) the same blocker-set hash has persisted >= prolonged_quiescence_hours of
      wall-clock (measured from hash_first_seen_at), AND
  (b) EVERY blocked goal is gated by a user-only blocker_ref type.

Pattern: importlib-load the gate module (same as test_quiescence_drainable.py),
monkey-patch the module-level AGENT_DIR to a per-test tmp dir so the throttle
dedup file (<agent>/session/prolonged-quiescence-pinged.json) reads/writes in a
sandbox. Real asserts (a bool-returning test passes vacuously under pytest).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

GATE_PATH = CORE_SCRIPTS / "quiescence-gate.py"
spec = importlib.util.spec_from_file_location("quiescence_gate", GATE_PATH)
qg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qg)

CFG = {"prolonged_quiescence_hours": 4.0, "prolonged_throttle_hours": 12.0}


def _set_agent_dir(tmp_path):
    """Point the module's AGENT_DIR at a tmp sandbox; ensure session/ exists
    (the throttle writer does tmp.write_text — parent must exist)."""
    (tmp_path / "session").mkdir(parents=True, exist_ok=True)
    qg.AGENT_DIR = tmp_path
    return tmp_path


def _entry(goal_id, external_id, btype="user_action", title=None):
    return {
        "goal_id": goal_id,
        "title": title or f"title-{goal_id}",
        "blocker_ref": {"type": btype, "external_id": external_id},
    }


def _iso_hours_ago(h):
    return (datetime.now() - timedelta(hours=h)).isoformat(timespec="seconds")


def _ping_file(tmp_path):
    return tmp_path / "session" / qg.PROLONGED_PING_NAME


# ---- (1) fires when prolonged + all-user-gated ----

def test_fires_when_prolonged_and_all_user_gated(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [_entry("g-1", "pq-X"), _entry("g-2", "pq-X")]
    out = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    assert out["prolonged_quiescence"] is True
    assert out["should_notify"] is True
    payload = out["prolonged_payload"]
    assert payload["highest_leverage_blocker_id"] == "pq-X"
    assert payload["blocker_count"] == 2
    assert payload["distinct_blocker_count"] == 1
    assert payload["hours_in_quiescence"] >= 4.0
    assert len(payload["sample_blocked_goal_titles"]) == 2
    # dedup file written with the hash recorded
    pf = _ping_file(tmp_path)
    assert pf.exists()
    assert "hashA" in json.loads(pf.read_text(encoding="utf-8"))


# ---- (2) throttled on second call, same hash, within window ----

def test_throttled_second_call_same_hash(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [_entry("g-1", "pq-X")]
    first = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    assert first["should_notify"] is True
    # second call: same hash, dedup file now carries a fresh ping (<12h) -> throttle
    second = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    assert second["prolonged_quiescence"] is True   # still prolonged...
    assert second["should_notify"] is False          # ...but throttled
    assert "throttled_hours_remaining" in second
    assert second["throttled_hours_remaining"] <= 12.0


# ---- (3) a DIFFERENT hash is not throttled by a prior hash's ping ----

def test_different_hash_not_throttled(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [_entry("g-1", "pq-X")]
    qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    other = qg._evaluate_prolonged_quiescence(
        entries, "hashB", _iso_hours_ago(5), datetime.now(), CFG)
    assert other["should_notify"] is True
    # both hashes now recorded in the dedup file
    recorded = json.loads(_ping_file(tmp_path).read_text(encoding="utf-8"))
    assert "hashA" in recorded and "hashB" in recorded


# ---- (4) under the wall-clock threshold -> no fire ----

def test_under_threshold_no_fire(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [_entry("g-1", "pq-X")]
    out = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(2), datetime.now(), CFG)
    assert out["prolonged_quiescence"] is False
    assert out["should_notify"] is False
    # nothing pinged
    assert not _ping_file(tmp_path).exists()


# ---- (5) mixed blocker types -> not all user-gated -> no fire ----

def test_mixed_blocker_types_no_fire(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [
        _entry("g-1", "pq-X", btype="user_action"),
        _entry("g-2", "svc-Y", btype="external-service"),  # NOT user-only
    ]
    out = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    assert out["prolonged_quiescence"] is False
    assert out["should_notify"] is False
    assert not _ping_file(tmp_path).exists()


# ---- edge: every user-only type qualifies ----

def test_all_user_only_types_qualify(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [
        _entry("g-1", "pq-A", btype="user_action"),
        _entry("g-2", "pq-B", btype="credentials-required"),
        _entry("g-3", "pq-C", btype="security-trust"),
        _entry("g-4", "pq-D", btype="physical-hardware"),
    ]
    out = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(6), datetime.now(), CFG)
    assert out["prolonged_quiescence"] is True
    assert out["should_notify"] is True


# ---- edge: empty entries -> default no-op ----

def test_empty_entries_no_fire(tmp_path):
    _set_agent_dir(tmp_path)
    out = qg._evaluate_prolonged_quiescence(
        [], "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    assert out["prolonged_quiescence"] is False
    assert out["should_notify"] is False


# ---- highest-leverage selection: the ext id gating the MOST goals wins ----

def test_highest_leverage_selection(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [
        _entry("g-1", "pq-BIG"),
        _entry("g-2", "pq-BIG"),
        _entry("g-3", "pq-BIG"),
        _entry("g-4", "pq-small"),
    ]
    out = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    payload = out["prolonged_payload"]
    assert payload["highest_leverage_blocker_id"] == "pq-BIG"
    assert payload["blocker_count"] == 4
    assert payload["distinct_blocker_count"] == 2
    # sample titles are drawn from the highest-leverage blocker's goals (<=5)
    assert len(payload["sample_blocked_goal_titles"]) == 3


# ---- missing blocker_ref (not a dict) -> not all user-gated -> no fire ----

def test_missing_blocker_ref_no_fire(tmp_path):
    _set_agent_dir(tmp_path)
    entries = [
        _entry("g-1", "pq-X"),
        {"goal_id": "g-2", "title": "no-ref", "blocker_ref": None},
    ]
    out = qg._evaluate_prolonged_quiescence(
        entries, "hashA", _iso_hours_ago(5), datetime.now(), CFG)
    assert out["prolonged_quiescence"] is False
    assert out["should_notify"] is False
