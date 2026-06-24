#!/usr/bin/env python3
"""Hermetic tests for core/scripts/inbox-backlog-update.py ().

Validates the inbox_alert_backlog counter end-to-end (compute + atomic write):
  - single pending Unblock alert goal  -> count==1 + oldest_goal_id + updated_at
  - two goals, unordered                -> count==2, older wins oldest_goal_id
  - zero matching goals                 -> field written as null
  - claimed / wrong-status / wrong-prefix / non-Unblock goals all excluded

Isolation (rb-1555): mkdtemp tmp-world + MIND_WORLD redirect (honored by
_paths.WORLD_DIR, which team-state.py derives TEAM_STATE_PATH from), subprocess
with child-only env, shutil.rmtree cleanup. The live world is never opened.

Dual-mode: `pytest` collects test_inbox_backlog_update; `python <file>` runs main().
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

CORE_SCRIPTS = Path(__file__).resolve().parents[1]          # core/scripts
SCRIPT = CORE_SCRIPTS / "inbox-backlog-update.py"


def _seed_world(tmp, goals):
    """Write tmp/aspirations.jsonl with asp-115 carrying `goals`."""
    asp = {"id": "asp-115", "title": "Central task list",
           "status": "active", "goals": goals}
    (tmp / "aspirations.jsonl").write_text(json.dumps(asp) + "\n",
                                           encoding="utf-8")


def _goal(gid, **over):
    g = {
        "id": gid,
        "title": f"Unblock: server {gid} down",
        "status": "pending",
        "origin_signal": f"alert-email:s3key-{gid}",
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "priority": "HIGH",
        "participants": ["agent"],
    }
    g.update(over)
    return g


def _run(tmp):
    """Invoke the companion with MIND_WORLD=tmp (child-only env)."""
    env = os.environ.copy()
    env["MIND_WORLD"] = str(tmp)
    env["MIND_AGENT"] = "alpha"
    return subprocess.run(
        [sys.executable, str(SCRIPT),
         "--aspiration", "asp-115",
         "--origin-prefix", "alert-email:",
         "--source", "world", "--print"],
        env=env, capture_output=True, text=True, timeout=90,
    )


def _read_backlog(tmp):
    p = tmp / "team-state.yaml"
    assert p.exists(), "team-state.yaml was not written"
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    # key must exist after a write (None for the zero-backlog case)
    assert "inbox_alert_backlog" in data, f"field absent: {list(data)}"
    return data["inbox_alert_backlog"]


def _case_single(tmp):
    _seed_world(tmp, [_goal("g-115-900")])
    r = _run(tmp)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    bl = _read_backlog(tmp)
    assert isinstance(bl, dict), f"expected dict, got {bl!r} (stdout={r.stdout})"
    assert bl.get("count") == 1, bl
    assert bl.get("oldest_goal_id") == "g-115-900", bl
    assert isinstance(bl.get("oldest_age_hours"), (int, float)), bl
    assert bl.get("oldest_age_hours") >= 0, bl
    ua = datetime.strptime(bl["updated_at"][:19], "%Y-%m-%dT%H:%M:%S")
    assert abs((datetime.now() - ua).total_seconds()) < 60, f"updated_at stale: {bl}"


def _case_oldest_wins(tmp):
    old = _goal("g-115-800",
                created_at=(datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S"))
    new = _goal("g-115-801",
                created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    _seed_world(tmp, [new, old])           # unordered on purpose
    r = _run(tmp)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    bl = _read_backlog(tmp)
    assert bl.get("count") == 2, bl
    assert bl.get("oldest_goal_id") == "g-115-800", bl
    assert bl.get("oldest_age_hours") >= 4.5, bl    # ~5h, allow rounding slack


def _case_null(tmp):
    _seed_world(tmp, [])
    r = _run(tmp)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    bl = _read_backlog(tmp)
    assert bl is None, f"expected null backlog, got {bl!r}"


def _case_exclusions(tmp):
    goals = [
        _goal("g-1", claimed_by="bravo"),                 # claimed -> excluded
        _goal("g-2", status="completed"),                  # wrong status -> excluded
        _goal("g-3", origin_signal="strategic-scan:x"),    # wrong prefix -> excluded
        _goal("g-4", title="Investigate: weird thing"),    # non-Unblock -> excluded
        _goal("g-5"),                                      # the only valid one
    ]
    _seed_world(tmp, goals)
    r = _run(tmp)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    bl = _read_backlog(tmp)
    assert isinstance(bl, dict), bl
    assert bl.get("count") == 1, bl
    assert bl.get("oldest_goal_id") == "g-5", bl


def main():
    cases = [_case_single, _case_oldest_wins, _case_null, _case_exclusions]
    for c in cases:
        tmp = Path(tempfile.mkdtemp(prefix="inbox-backlog-test-"))
        try:
            c(tmp)
            print(f"PASS {c.__name__}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("ALL PASS")
    return 0


def test_inbox_backlog_update():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
