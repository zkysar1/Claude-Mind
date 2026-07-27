"""test_team_state_current_focus.py — regression for .

cmd_in_flight (CLI) and the daemon in_flight() endpoint now stamp
agent_status.<agent>.current_focus (+ current_focus_updated_at) at claim time
so partner Theory-of-Mind beliefs track the actual lane instead of inferring
from lagging completions. Before this, current_focus was populated only
inconsistently (some agents hand-set it, most left it empty), so partners read
an empty observation and fell back to completion-inference (~26-41h stale).

The stamped value is the LANE: aspiration parsed from goal_id (g-NNN-MM ->
asp-NNN) + the goal title, e.g. "asp-115: Recurring: sweep X". Fallbacks: bare
aspiration when no title; bare title when goal_id is unparseable.

These tests exercise the CLI path via subprocess against an ISOLATED tmp world
(MIND_WORLD override — never touches live team-state.yaml). The daemon mirror
(mind_api/src/world/team_state_write.py) is byte-identical by construction
(guard-742 dual-write) and is exercised by the daemon-aware wrapper suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(2)

TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"


def _run_in_flight(world: Path, agent: str, goal_id: str, title: str,
                   phase: str = "4") -> subprocess.CompletedProcess:
    """Run `team-state.py in-flight` against an isolated tmp world."""
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(world)
    cmd = [sys.executable, str(TEAM_STATE_PY), "in-flight",
           "--agent", agent, "--goal-id", goal_id, "--title", title,
           "--phase", phase]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=30)


def _read_entry(world: Path, agent: str) -> dict:
    #  sharding: the claim stamp lands in the agent's ROW file
    # (world/team-state/agents/<agent>.yaml); core file only holds residuals.
    row = world / "team-state" / "agents" / f"{agent}.yaml"
    if row.is_file():
        return yaml.safe_load(row.read_text(encoding="utf-8")) or {}
    core = world / "team-state.yaml"
    if not core.is_file():
        return {}
    state = yaml.safe_load(core.read_text(encoding="utf-8")) or {}
    return (state.get("agent_status") or {}).get(agent) or {}


def test_in_flight_stamps_current_focus():
    """Normal case: current_focus == "<asp>: <title>" + timestamp present."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run_in_flight(world, "delta", "g-115-105", "Recurring sweep")
        assert r.returncode == 0, r.stderr
        entry = _read_entry(world, "delta")
        assert entry.get("current_focus") == "asp-115: Recurring sweep", entry
        assert entry.get("current_focus_updated_at"), entry
        # in_flight + last_active still stamped (no regression).
        assert entry["in_flight"]["goal_id"] == "g-115-105", entry
        assert entry.get("last_active"), entry


def test_current_focus_no_title_falls_back_to_aspiration():
    """Empty title -> current_focus is the bare aspiration lane."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run_in_flight(world, "delta", "g-309-04", "")
        assert r.returncode == 0, r.stderr
        entry = _read_entry(world, "delta")
        assert entry.get("current_focus") == "asp-309", entry


def test_current_focus_unparseable_goal_id_falls_back_to_title():
    """goal_id not matching g-NNN-MM -> current_focus is the title alone."""
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run_in_flight(world, "delta", "adhoc-task", "Some focus")
        assert r.returncode == 0, r.stderr
        entry = _read_entry(world, "delta")
        assert entry.get("current_focus") == "Some focus", entry


if __name__ == "__main__":
    test_in_flight_stamps_current_focus()
    test_current_focus_no_title_falls_back_to_aspiration()
    test_current_focus_unparseable_goal_id_falls_back_to_title()
    print("ok")
