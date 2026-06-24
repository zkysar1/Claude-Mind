"""test_wm_contamination_check.py -  cross-agent WM contamination detector.

Exercises core/scripts/wm-contamination-check.py end-to-end via subprocess
against a SYNTHETIC temp PROJECT_ROOT (never the live repo, world dir, or any
agent's real WM). Each case lays out agents/<agent>/session/working-memory.yaml,
a synthetic world aspirations.jsonl (the ownership index), and a board
coordination.jsonl, then runs the real detector with --json and asserts the
verdict (and, on the contamination path, the quarantine side effects).

Verification matrix (mirrors the goal's required outcomes):
  - contamination fires:        foreign-dominated WM -> quarantine + fresh WM
  - fresh template valid:       post-quarantine WM parses, empty, foreign gone;
                                quarantined file retains the original payload
  - collab (own dominates):     mostly own goals + a few foreign -> NO fire
  - collab (board guard):       foreign goals the bound agent claimed on the
                                board -> NO fire (the critical false-positive guard)
  - recurring excluded:         foreign-completed RECURRING goals -> NO fire
                                (recurring completed_by is who-ran-it-last, not owner)
  - no bound agent / no WM:     graceful no-op (exit 0, is_contaminated False)

Live-daemon safe (guard-672): pure filesystem + a Python subprocess against a
tmp dir; never touches the live daemon or any real data root.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REAL_SCRIPT = CORE_SCRIPTS / "wm-contamination-check.py"

AGENT = "bravo"
SOURCE = "zeta"


def _make_wm(completed_ids: list) -> dict:
    """A WM whose two authoritative completed-goal lists carry `completed_ids`."""
    return {
        "encoding_queue": [],
        "session_id": None,
        "session_start": "2026-06-23T00:00:00",
        "goals_completed_this_session": [
            {"goal_id": g, "aspiration_id": "asp-900", "recurring": False}
            for g in completed_ids
        ],
        "aspiration_touched_last": "asp-900",
        "last_goal_category": "framework",
        "slots": {
            "micro_hypotheses": [],
            "loop_state": {
                "goals_completed": len(completed_ids),
                "productive_goals": len(completed_ids),
                "counted_goals_this_session": list(completed_ids),
                "routine_streaks": {},
                "touched": ["asp-900"],
            },
        },
        "slot_meta": {},
    }


def _world_asp(goal_specs: list) -> str:
    """One world aspiration line whose goals carry id/completed_by/recurring."""
    goals = []
    for gid, completed_by, recurring in goal_specs:
        goals.append({
            "id": gid,
            "title": f"goal {gid}",
            "status": "completed" if completed_by else "pending",
            "recurring": recurring,
            "completed_by": completed_by,
            "claimed_by": None,
        })
    return json.dumps({"id": "asp-900", "title": "synthetic", "goals": goals})


def _board_claim(goal_id: str, author: str, when: datetime) -> str:
    return json.dumps({
        "id": f"msg-{goal_id}",
        "author": author,
        "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S"),
        "channel": "coordination",
        "type": "claim",
        "text": f"Claimed: {goal_id}",
        "reply_to": None,
        "tags": [goal_id, "asp-900"],
    })


def _build_root(tmp: Path, *, wm: dict, world_goal_specs: list,
                board_lines: list | None = None) -> tuple:
    """Construct a synthetic PROJECT_ROOT + external world dir.

    Returns (root, world_dir). The agent WM lives at
    agents/<AGENT>/session/working-memory.yaml; the ownership index lives at
    <world_dir>/aspirations.jsonl; board posts (if any) at
    <world_dir>/board/coordination.jsonl.
    """
    root = tmp / "proj"
    wm_dir = root / "agents" / AGENT / "session"
    wm_dir.mkdir(parents=True)
    with open(wm_dir / "working-memory.yaml", "w", encoding="utf-8") as f:
        yaml.dump(wm, f, default_flow_style=False, sort_keys=False)

    world_dir = tmp / "ext_world"
    world_dir.mkdir()
    (world_dir / "aspirations.jsonl").write_text(
        _world_asp(world_goal_specs) + "\n", encoding="utf-8")

    if board_lines:
        bdir = world_dir / "board"
        bdir.mkdir()
        (bdir / "coordination.jsonl").write_text(
            "\n".join(board_lines) + "\n", encoding="utf-8")

    return root, world_dir


def _run(root: Path, world_dir: Path, *flags: str, agent: str = AGENT):
    cmd = [
        sys.executable, str(REAL_SCRIPT),
        "--agent", agent,
        "--project-root", str(root),
        "--world-dir", str(world_dir),
        "--json", *flags,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"detector exited {r.returncode}: {r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- #

def test_contamination_fires_and_quarantines():
    foreign = [f"g-901-{i:02d}" for i in range(1, 11)]  # 10 zeta-completed goals
    with tempfile.TemporaryDirectory(prefix="wmc-contam-") as tmpd:
        root, world_dir = _build_root(
            Path(tmpd),
            wm=_make_wm(foreign),
            world_goal_specs=[(g, SOURCE, False) for g in foreign],
            board_lines=None,  # bravo has NO board posts for these goals
        )
        res = _run(root, world_dir, "--apply")
        assert res["is_contaminated"] is True, res
        assert res["dominant_source"] == SOURCE, res
        assert res["dominant_goal_count"] == 10, res
        assert res["own_count"] == 0, res
        assert res["action"] == "quarantined", res

        # Quarantine side effects.
        q_path = Path(res["quarantine_path"])
        assert q_path.exists(), "quarantined file missing"
        # The quarantined file retains the original (contaminated) payload.
        q_wm = yaml.safe_load(q_path.read_text(encoding="utf-8"))
        q_ids = [e["goal_id"] for e in q_wm["goals_completed_this_session"]]
        assert set(q_ids) == set(foreign), q_ids


def test_fresh_template_valid_after_quarantine():
    foreign = [f"g-902-{i:02d}" for i in range(1, 11)]
    with tempfile.TemporaryDirectory(prefix="wmc-fresh-") as tmpd:
        root, world_dir = _build_root(
            Path(tmpd),
            wm=_make_wm(foreign),
            world_goal_specs=[(g, SOURCE, False) for g in foreign],
        )
        res = _run(root, world_dir, "--apply")
        assert res["action"] == "quarantined", res

        wm_path = root / "agents" / AGENT / "session" / "working-memory.yaml"
        fresh = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
        # Clean, parseable, foreign payload gone.
        assert fresh["goals_completed_this_session"] == [], fresh
        assert isinstance(fresh["slots"], dict)
        assert "loop_state" not in fresh["slots"], "fresh WM must not carry loop_state"
        assert fresh["encoding_queue"] == []
        # Base slots present (mirror of wm.py default template).
        assert "micro_hypotheses" in fresh["slots"]
        assert fresh["slots"]["micro_hypotheses"] == []


def test_collab_own_dominates_no_false_positive():
    own = [f"g-903-{i:02d}" for i in range(1, 13)]   # 12 bravo-completed
    foreign = [f"g-904-{i:02d}" for i in range(1, 3)]  # 2 zeta-completed (collab)
    with tempfile.TemporaryDirectory(prefix="wmc-own-") as tmpd:
        specs = [(g, AGENT, False) for g in own] + [(g, SOURCE, False) for g in foreign]
        root, world_dir = _build_root(
            Path(tmpd), wm=_make_wm(own + foreign), world_goal_specs=specs)
        res = _run(root, world_dir, "--apply")
        assert res["is_contaminated"] is False, res
        assert res["own_count"] == 12, res
        assert res["action"] == "none", res
        # WM untouched (still carries the original lists).
        wm_path = root / "agents" / AGENT / "session" / "working-memory.yaml"
        wm = yaml.safe_load(wm_path.read_text(encoding="utf-8"))
        assert len(wm["goals_completed_this_session"]) == 14, wm


def test_collab_board_guard_no_false_positive():
    """The critical guard: foreign-COMPLETED goals the bound agent CLAIMED on the
    board are legit handoffs, not contamination. own_count is 0 here, so ONLY the
    board guard can suppress the false positive."""
    # >=8 goals so the cheap thresholds (min_attrib=8) implicate a block and the
    # board guard is actually reached; ALL claimed by the bound agent -> cleared.
    foreign = [f"g-905-{i:02d}" for i in range(1, 11)]  # 10 zeta-completed
    recent = datetime.now() - timedelta(days=1)
    board = [_board_claim(g, AGENT, recent) for g in foreign]  # bravo claimed all 10
    with tempfile.TemporaryDirectory(prefix="wmc-board-") as tmpd:
        root, world_dir = _build_root(
            Path(tmpd),
            wm=_make_wm(foreign),
            world_goal_specs=[(g, SOURCE, False) for g in foreign],
            board_lines=board,
        )
        res = _run(root, world_dir, "--apply")
        assert res["board_implicated"] is True, res   # thresholds DID implicate
        assert res["board_clears"] is True, res        # ...but the board cleared it
        assert res["is_contaminated"] is False, res
        assert res["action"] == "none", res


def test_recurring_excluded_no_false_positive():
    """Foreign-completed RECURRING goals are shared-queue cadence, not ownership.
    They must be excluded from the tally entirely."""
    recurring_foreign = [f"g-001-{i:02d}" for i in range(1, 9)]  # 8 recurring, completed_by zeta
    with tempfile.TemporaryDirectory(prefix="wmc-recur-") as tmpd:
        root, world_dir = _build_root(
            Path(tmpd),
            wm=_make_wm(recurring_foreign),
            world_goal_specs=[(g, SOURCE, True) for g in recurring_foreign],
        )
        res = _run(root, world_dir, "--apply")
        assert res["is_contaminated"] is False, res
        assert res["dominant_goal_count"] == 0, res
        assert res["recurring_skipped"] == 8, res
        assert res["action"] == "none", res


def test_no_bound_agent_noop():
    with tempfile.TemporaryDirectory(prefix="wmc-noagent-") as tmpd:
        root = Path(tmpd) / "proj"
        (root / "agents").mkdir(parents=True)
        world_dir = Path(tmpd) / "ext_world"
        world_dir.mkdir()
        # Empty --agent resolves to no bound agent.
        res = _run(root, world_dir, agent="")
        assert res["status"] == "no-bound-agent", res
        assert res["is_contaminated"] is False, res


def test_no_wm_noop():
    with tempfile.TemporaryDirectory(prefix="wmc-nowm-") as tmpd:
        root = Path(tmpd) / "proj"
        (root / "agents" / AGENT).mkdir(parents=True)  # agent dir but no session/WM
        world_dir = Path(tmpd) / "ext_world"
        world_dir.mkdir()
        res = _run(root, world_dir)
        assert res["status"] == "no-wm", res
        assert res["is_contaminated"] is False, res


if __name__ == "__main__":
    test_contamination_fires_and_quarantines()
    test_fresh_template_valid_after_quarantine()
    test_collab_own_dominates_no_false_positive()
    test_collab_board_guard_no_false_positive()
    test_recurring_excluded_no_false_positive()
    test_no_bound_agent_noop()
    test_no_wm_noop()
    print("PASS: g-303-22 wm-contamination-check detector")
