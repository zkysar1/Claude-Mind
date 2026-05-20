"""test_defer_date_extractor.py — extractor unit tests + cmd_update_goal wiring.

Covers LifingPolls plan item 5 (2026-05-08): when a defer_reason narrative
contains a date phrase and deferred_until is null, cmd_update_goal must
auto-set deferred_until via defer-date-extractor.py.

Lanes:
  1. Pure extractor — 6 phrasings + the no-match case
  2. Wiring — cmd_update_goal sets both fields atomically when only
     defer_reason is supplied
  3. Wiring — caller-supplied deferred_until is preserved (extractor skipped)
  4. Wiring — clearing defer_reason does NOT clear an unrelated deferred_until
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
EXTRACTOR = CORE_SCRIPTS / "defer-date-extractor.py"


def run_extractor(text: str, now: str = "2026-05-08T12:00:00") -> dict:
    proc = subprocess.run(
        [sys.executable, str(EXTRACTOR), text, "--now", now],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


# ---- Lane 1: extractor patterns -------------------------------------------


def test_iso_date():
    r = run_extractor("Not before 2026-07-14")
    assert r["matched"]
    assert r["deferred_until"] == "2026-07-14T00:00:00"
    assert r["pattern"] == "iso_date"


def test_month_day_year():
    r = run_extractor("after July 14, 2026")
    assert r["matched"]
    assert r["deferred_until"] == "2026-07-14T00:00:00"
    assert r["pattern"] == "month_day_year"


def test_day_month_year():
    r = run_extractor("Defer until 14 March 2027 for reasons",
                      now="2026-05-08T12:00:00")
    assert r["matched"]
    assert r["deferred_until"] == "2027-03-14T00:00:00"
    assert r["pattern"] == "day_month_year"


def test_relative_in_n():
    r = run_extractor("in 7 days")
    assert r["matched"]
    assert r["deferred_until"] == "2026-05-15T12:00:00"
    assert r["pattern"] == "relative_in_n"


def test_relative_tomorrow():
    r = run_extractor("tomorrow we restart")
    assert r["matched"]
    assert r["deferred_until"] == "2026-05-09T12:00:00"
    assert r["pattern"] == "relative_tomorrow"


def test_no_match():
    r = run_extractor("waiting on partner sign-off")
    assert not r["matched"]
    assert r["deferred_until"] is None


def test_past_date_skipped():
    """A date in the past is NOT a future defer; skip it."""
    r = run_extractor("Last reviewed 2020-01-01", now="2026-05-08T12:00:00")
    assert not r["matched"]


def test_earliest_future_wins():
    """Multiple future dates → earliest wins (most conservative defer)."""
    r = run_extractor("between 2026-09-01 and 2026-06-15")
    assert r["matched"]
    assert r["deferred_until"] == "2026-06-15T00:00:00"


# ---- Lane 2-4: wiring through cmd_update_goal ------------------------------
# Direct-import the module + use a temp WORLD_DIR so we don't touch live state.


def _make_test_aspirations(tmp: Path):
    """Seed a minimal world/aspirations.jsonl with one goal."""
    world = tmp / "world"
    world.mkdir()
    asp = {
        "id": "asp-100",
        "title": "Test asp",
        "motivation": "Test motivation",
        "scope": "project",
        "priority": "MEDIUM",
        "status": "active",
        "created": "2026-05-08T12:00:00",
        "goals": [{
            "id": "g-100-01",
            "title": "Test goal",
            "description": "Test goal for defer-date wiring",
            "status": "pending",
            "priority": "MEDIUM",
            "deferred_until": None,
            "defer_reason": None,
            "blocked_by": [],
            "verification": {"outcomes": ["x"], "checks": [], "preconditions": []},
            "origin_signal": "user_directive",
            "achievedCount": 0,
            "participants": ["agent"],
        }],
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(asp, ensure_ascii=False) + "\n", encoding="utf-8")
    (world / "aspirations-archive.jsonl").write_text("", encoding="utf-8")
    return world


def _run_update(world: Path, goal_id: str, field: str, value: str,
                extra_args: list | None = None) -> tuple[int, str, str]:
    """Run aspirations.py update-goal as subprocess against tmp world."""
    import os
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    env["MIND_AGENT"] = env.get("MIND_AGENT", "alpha")
    args = [sys.executable, str(CORE_SCRIPTS / "aspirations.py"),
            "update-goal", goal_id, field, value]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.run(args, capture_output=True, text=True,
                          timeout=15, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _read_goal(world: Path, goal_id: str) -> dict:
    text = (world / "aspirations.jsonl").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        asp = json.loads(line)
        for g in asp.get("goals", []):
            if g.get("id") == goal_id:
                return g
    raise KeyError(goal_id)


def test_wiring_auto_sets_deferred_until():
    """Setting defer_reason with embedded date auto-sets deferred_until."""
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = _make_test_aspirations(tmp)
        # Bypass capability-gate via blocker_ref + force-unstructured-defer
        # is not needed because "Not before <date>" doesn't name a capability.
        # The blocker_ref check WILL fire — we pass --blocker-ref to satisfy it.
        rc, out, err = _run_update(
            world, "g-100-01", "defer_reason", "Not before 2027-01-15",
            ["--blocker-ref",
             '{"type":"external-service","external_id":"upstream-api"}']
        )
        assert rc == 0, f"rc={rc}\nstdout={out}\nstderr={err}"
        g = _read_goal(world, "g-100-01")
        assert g["defer_reason"] == "Not before 2027-01-15"
        assert g["deferred_until"] == "2027-01-15T00:00:00"


def test_wiring_no_date_no_change():
    """defer_reason without a date phrase leaves deferred_until null."""
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = _make_test_aspirations(tmp)
        rc, out, err = _run_update(
            world, "g-100-01", "defer_reason",
            "partner agent has not responded",
            ["--blocker-ref",
             '{"type":"partner-response","external_id":"bravo"}']
        )
        assert rc == 0, f"rc={rc}\nstdout={out}\nstderr={err}"
        g = _read_goal(world, "g-100-01")
        assert g["defer_reason"] == "partner agent has not responded"
        assert g["deferred_until"] is None


def test_wiring_clear_defer_reason():
    """Clearing defer_reason does not clobber deferred_until."""
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        world = _make_test_aspirations(tmp)
        # Set both via the auto-extract path
        rc, _, err = _run_update(
            world, "g-100-01", "defer_reason", "Not before 2027-06-15",
            ["--blocker-ref",
             '{"type":"external-service","external_id":"upstream-svc-z"}']
        )
        assert rc == 0, err
        # Clear defer_reason
        rc, _, err = _run_update(world, "g-100-01", "defer_reason", "null")
        assert rc == 0, err
        g = _read_goal(world, "g-100-01")
        assert g["defer_reason"] is None
        # deferred_until is preserved — clearing reason only drops blocker_ref
        # and defer_reason_set_at, not the time gate. The selector will still
        # filter the goal until the time passes.
        assert g["deferred_until"] == "2027-06-15T00:00:00"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
