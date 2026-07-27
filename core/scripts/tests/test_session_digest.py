"""test_session_digest.py --  (asp-317, FW-9).

Pins session-digest.py: the unified read-only "what changed since last session"
orientation digest. Each test seeds synthetic world/ + agent/ dirs in tmp, runs
the script as a subprocess with --world-dir/--agent-dir/--agent/--now/--no-commits
(deterministic windowing, hermetic git skip), and asserts the emitted digest.

Load-bearing properties pinned here:
  - FAIL-OPEN: empty/missing sources -> stub sections, exit 0 (never crash).
  - own-goals ownership filter: agent-queue goals all count; world-queue goals
    count ONLY when claimed_by == this agent.
  - board window filter: posts older than --since-hours are excluded.
  - team-state: is_self flag + age + in_flight surfaced.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "session-digest.py"
PYTHON = sys.executable

NOW = "2026-06-14T12:00:00"            # pinned upper bound
IN_WINDOW = "2026-06-14T09:00:00"      # 3h before NOW (default 24h window)
OUT_OF_WINDOW = "2026-06-12T09:00:00"  # >24h before NOW


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_jsonl(path: Path, records):
    _write(path, "".join(json.dumps(r) + "\n" for r in records))


def _seed(tmp_path, agent_asps=None, world_asps=None, journal=None,
          coord=None, findings=None, team_state=None, handoff=None):
    world = tmp_path / "world"
    agent = tmp_path / "agent"
    _write_jsonl(world / "aspirations.jsonl", world_asps or [])
    _write_jsonl(agent / "aspirations.jsonl", agent_asps or [])
    _write_jsonl(agent / "journal.jsonl", journal or [])
    _write_jsonl(world / "board" / "coordination.jsonl", coord or [])
    _write_jsonl(world / "board" / "findings.jsonl", findings or [])
    if team_state is not None:
        _write(world / "team-state.yaml", team_state)
    if handoff is not None:
        _write(agent / "session" / "handoff.yaml", handoff)
    return world, agent


def _run(world, agent, *extra):
    r = subprocess.run(
        [PYTHON, str(SCRIPT), "--world-dir", str(world), "--agent-dir", str(agent),
         "--agent", "delta", "--no-commits", "--now", NOW, "--format", "json", *extra],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"rc={r.returncode} stderr={r.stderr}"
    return json.loads(r.stdout)


def _goal(gid, status, claimed_by=None, title="t"):
    g = {"id": gid, "title": title, "status": status}
    if claimed_by is not None:
        g["claimed_by"] = claimed_by
    return g


def test_empty_inputs_exit_zero(tmp_path):
    """Missing/empty sources -> stub sections, clean exit (fail-open)."""
    world, agent = _seed(tmp_path)
    out = _run(world, agent)
    assert out["agent"] == "delta"
    s = out["sections"]
    assert s["handoff"] == {"present": False}
    assert s["own_goals"]["in_progress"] == []
    assert s["own_goals"]["pending_count"] == 0
    assert s["team_state"] == {"present": False}
    assert s["journal"] == []


def test_own_goals_ownership_filter(tmp_path):
    """Agent-queue open goals all count; world-queue goals count ONLY when
    claimed_by == this agent."""
    world, agent = _seed(
        tmp_path,
        agent_asps=[{"id": "asp-900", "goals": [
            _goal("g-900-1", "in-progress"),
            _goal("g-900-2", "pending"),
            _goal("g-900-3", "completed"),   # excluded (terminal)
        ]}],
        world_asps=[{"id": "asp-901", "goals": [
            _goal("g-901-1", "in-progress", claimed_by="delta"),   # own
            _goal("g-901-2", "in-progress", claimed_by="alpha"),   # NOT own
            _goal("g-901-3", "pending"),                            # unclaimed -> not own
        ]}],
    )
    og = _run(world, agent)["sections"]["own_goals"]
    ip_ids = {g["id"] for g in og["in_progress"]}
    assert ip_ids == {"g-900-1", "g-901-1"}     # alpha's + unclaimed excluded
    assert og["pending_count"] == 1             # only the agent-queue pending


def test_archived_aspiration_excluded(tmp_path):
    """Goals under an archived aspiration are not counted as own work."""
    world, agent = _seed(tmp_path, agent_asps=[
        {"id": "asp-902", "archived": True, "goals": [_goal("g-902-1", "in-progress")]}])
    og = _run(world, agent)["sections"]["own_goals"]
    assert og["in_progress"] == []


def test_board_window_filter(tmp_path):
    """Posts older than the window are excluded; in-window posts kept."""
    world, agent = _seed(tmp_path, coord=[
        {"timestamp": OUT_OF_WINDOW, "author": "bravo", "type": "status", "text": "old"},
        {"timestamp": IN_WINDOW, "author": "alpha", "type": "claim", "text": "fresh"},
    ])
    board = _run(world, agent)["sections"]["board"]
    texts = [p["text"] for p in board["coordination"]]
    assert texts == ["fresh"]


def test_team_state_self_age_inflight(tmp_path):
    """team-state surfaces is_self, age_min, and in_flight."""
    ts = (
        "agent_status:\n"
        "  delta:\n"
        f"    last_active: '{IN_WINDOW}'\n"
        "    current_focus: framework\n"
        "  alpha:\n"
        f"    last_active: '{IN_WINDOW}'\n"
        "    in_flight:\n"
        "      goal_id: g-305-05\n"
        "      phase: '4'\n"
        "      title: seed pipeline\n"
    )
    world, agent = _seed(tmp_path, team_state=ts)
    state = _run(world, agent)["sections"]["team_state"]
    assert state["present"] is True
    assert state["agents"]["delta"]["is_self"] is True
    assert state["agents"]["alpha"]["is_self"] is False
    assert state["agents"]["delta"]["age_min"] == 180   # 3h before NOW
    assert state["agents"]["alpha"]["in_flight"]["goal_id"] == "g-305-05"


def test_handoff_present(tmp_path):
    """A present handoff.yaml surfaces with present:true + known fields."""
    handoff = ("current_focus: building digest\n"
               "next_action: run tests\n"
               "known_blockers_active: []\n")
    world, agent = _seed(tmp_path, handoff=handoff)
    h = _run(world, agent)["sections"]["handoff"]
    assert h["present"] is True
    assert h["current_focus"] == "building digest"
    assert h["known_blockers_active"] == 0   # empty list -> count 0


def test_journal_tail_and_malformed_tolerated(tmp_path):
    """Journal tail returns the most recent entries; a malformed line is skipped."""
    world, agent = _seed(tmp_path, journal=[
        {"date": "2026-06-13", "type": "reflection", "summary": "a"},
        {"date": "2026-06-14", "type": "goal", "summary": "b"},
    ])
    # Append a malformed tail line.
    with (agent / "journal.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"date": "2026-06-14", BROKEN\n')
    j = _run(world, agent, "--max-items", "5")["sections"]["journal"]
    assert [e["summary"] for e in j] == ["a", "b"]   # malformed skipped, order kept


def test_text_format_smoke(tmp_path):
    """--format text emits the header and exits 0 (no crash on real render)."""
    world, agent = _seed(tmp_path, agent_asps=[
        {"id": "asp-903", "goals": [_goal("g-903-1", "in-progress", title="x")]}])
    r = subprocess.run(
        [PYTHON, str(SCRIPT), "--world-dir", str(world), "--agent-dir", str(agent),
         "--agent", "delta", "--no-commits", "--now", NOW, "--format", "text"],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "session-digest: delta" in r.stdout
    assert "g-903-1" in r.stdout
