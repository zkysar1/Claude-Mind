"""Unit tests for the  team-aware cadence gate in
fresh-eyes-cadence-check.py.

Shared-resource rituals (fresh_eyes_tree, fresh_eyes_program) have ONE time
series, but the cadence gate keyed only on the PER-AGENT WM slot — a teammate's
fire never advanced this agent's slot, so the ritual re-fired days after the
team already reviewed (canonical incident: delta re-fired the 200-goal tree
review 24h after the 2026-06-09 team review at goal 3830).

Fix under test: config blocks declaring `team_aware: true` consult
world/team-state.yaml shared_cadences.<wm_slot> inside the fire branch and
noop while the team's last fire is within cadence. Units are WORLD-ONLY goal
counts on both sides (agent-inclusive counts differ per agent and are not
cross-agent comparable).

Coverage:
  - noop when team stamp fresh (world diff < cadence), no slot write
  - fire when team stamp stale (world diff >= cadence)
  - fire when no team stamp exists (first team fire)
  - team-state NOT consulted when block lacks team_aware (backward compat)
  - fail-open on malformed stamp (missing/non-int count field)
  - team_stamp_value: reads shared_cadences.<slot>; None on missing/malformed
  - count_completed_goals(world_only=True) skips agent-queue files

Tests use monkeypatch + tmp_path so no live state is touched; subprocess.run
is stubbed so the suite is safe to run against a live daemon.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = PROJECT_ROOT / "core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def _load_cadence_module():
    """Load fresh-eyes-cadence-check.py as a module despite the hyphenated name."""
    spec = importlib.util.spec_from_file_location(
        "fresh_eyes_cadence_check_team",
        str(SCRIPT_DIR / "fresh-eyes-cadence-check.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_cadence_module()


# ── harness ───────────────────────────────────────────────────────────────────

def _stub_config(team_aware=True, goal_cadence=25, min_session_goals=0):
    block = {
        "goal_cadence": goal_cadence,
        "wm_slot": "last_fresh_eyes_review",
        "min_session_goals": min_session_goals,
    }
    if team_aware:
        block["team_aware"] = True
    return {"fresh_eyes_review": block}


def _wire(monkeypatch, mod, *, config, current, current_world, slot_value,
          team_stamp="UNSET"):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: config)
    monkeypatch.setattr(
        mod, "count_completed_goals",
        lambda world_only=False: current_world if world_only else current,
    )

    def fake_wm(slot_name):
        if slot_name == "loop_state":
            return {}
        if slot_name == "last_fresh_eyes_review":
            return slot_value
        return None

    monkeypatch.setattr(mod, "wm_slot_value", fake_wm)
    if team_stamp != "UNSET":
        monkeypatch.setattr(mod, "team_stamp_value", lambda _slot: team_stamp)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])


def _stub_subprocess(monkeypatch, mod):
    calls = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "input": kw.get("input")})

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""

        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


# ── main() team-aware branch ──────────────────────────────────────────────────

def test_main_noops_when_team_stamp_fresh(mod, monkeypatch, capsys):
    """Per-agent diff >= cadence but the team fired recently (world diff <
    cadence) → noop, no slot write."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(team_aware=True),
        current=100,            # per-agent diff = 100-70 = 30 >= 25 → would fire
        current_world=80,       # world diff = 80-65 = 15 < 25 → team gate noops
        slot_value={"timestamp": "2026-06-01T00:00:00", "goals_count_at_last_fire": 70},
        team_stamp={
            "timestamp": "2026-06-09T12:00:00",
            "world_goals_count_at_last_fire": 65,
            "fired_by": "bravo",
        },
    )
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, f"expected team-aware noop (rc=1), got rc={rc}; out={out!r}"
    assert "team-aware" in out
    assert "g-115-1388" in out
    assert "bravo" in out
    assert len(calls) == 0, "team-aware noop must not write any slot"


def test_main_fires_when_team_stamp_stale(mod, monkeypatch, capsys):
    """Team stamp exists but the world count has elapsed past cadence → fire."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(team_aware=True),
        current=100,
        current_world=95,       # world diff = 95-65 = 30 >= 25 → team gate passes
        slot_value={"timestamp": "2026-06-01T00:00:00", "goals_count_at_last_fire": 70},
        team_stamp={
            "timestamp": "2026-05-01T12:00:00",
            "world_goals_count_at_last_fire": 65,
            "fired_by": "alpha",
        },
    )
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected fire (rc=0), got rc={rc}; out={out!r}"
    assert "fire" in out
    assert len(calls) == 0


def test_main_fires_when_no_team_stamp(mod, monkeypatch, capsys):
    """team_aware block but no stamp yet (first team fire) → fire normally."""
    _wire(
        monkeypatch, mod,
        config=_stub_config(team_aware=True),
        current=100,
        current_world=95,
        slot_value={"timestamp": "2026-06-01T00:00:00", "goals_count_at_last_fire": 70},
        team_stamp=None,
    )
    calls = _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    assert rc == 0, "no team stamp → per-agent behavior (fire)"
    assert len(calls) == 0


def test_main_ignores_team_state_without_flag(mod, monkeypatch):
    """Backward compat: a block WITHOUT team_aware never consults team-state."""
    def _boom(_slot):
        raise AssertionError("team_stamp_value must not be called without team_aware")

    _wire(
        monkeypatch, mod,
        config=_stub_config(team_aware=False),
        current=100,
        current_world=80,
        slot_value={"timestamp": "2026-06-01T00:00:00", "goals_count_at_last_fire": 70},
    )
    monkeypatch.setattr(mod, "team_stamp_value", _boom)
    _stub_subprocess(monkeypatch, mod)

    rc = mod.main()
    assert rc == 0, "non-team-aware block fires exactly as before"


def test_main_fails_open_on_malformed_stamp(mod, monkeypatch):
    """Stamp present but count field missing or non-int → fire (fail-open)."""
    for bad_stamp in (
        {"timestamp": "2026-06-09T12:00:00", "fired_by": "bravo"},          # no count
        {"world_goals_count_at_last_fire": "not-a-number"},                  # non-int
        {},                                                                  # empty
    ):
        _wire(
            monkeypatch, mod,
            config=_stub_config(team_aware=True),
            current=100,
            current_world=80,
            slot_value={"timestamp": "2026-06-01T00:00:00",
                        "goals_count_at_last_fire": 70},
            team_stamp=bad_stamp,
        )
        _stub_subprocess(monkeypatch, mod)
        rc = mod.main()
        assert rc == 0, f"malformed stamp {bad_stamp!r} must fail open to fire"


# ── team_stamp_value ──────────────────────────────────────────────────────────

def test_team_stamp_value_reads_shared_cadences(mod, monkeypatch):
    doc = {
        "shared_cadences": {
            "last_fresh_eyes_tree_review": {
                "timestamp": "2026-06-09T12:00:00",
                "world_goals_count_at_last_fire": 3000,
                "fired_by": "bravo",
            }
        }
    }
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: doc)
    stamp = mod.team_stamp_value("last_fresh_eyes_tree_review")
    assert stamp == doc["shared_cadences"]["last_fresh_eyes_tree_review"]


def test_team_stamp_value_none_on_missing_or_malformed(mod, monkeypatch):
    for doc in (
        None,                                        # file unreadable
        {},                                          # no shared_cadences
        {"shared_cadences": "not-a-dict"},           # malformed container
        {"shared_cadences": {}},                     # slot absent
        {"shared_cadences": {"slot": "not-a-dict"}}, # malformed stamp
    ):
        monkeypatch.setattr(mod, "_load_yaml", lambda _p, _d=doc: _d)
        assert mod.team_stamp_value("slot") is None


# ── count_completed_goals(world_only=...) ─────────────────────────────────────

def _write_asp(path: Path, completed: int):
    rec = {"id": "asp-x", "goals": [{"status": "completed"}] * completed}
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def test_count_completed_goals_world_only_skips_agent(mod, monkeypatch, tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    agent = tmp_path / "agents" / "testagent"
    agent.mkdir(parents=True)
    _write_asp(world / "aspirations.jsonl", 2)
    _write_asp(agent / "aspirations.jsonl", 3)

    monkeypatch.setenv("MIND_AGENT", "testagent")
    monkeypatch.setattr(mod._paths, "WORLD_DIR", world)
    monkeypatch.setattr(mod._paths, "agent_dir", lambda _name: agent)

    assert mod.count_completed_goals() == 5, "default includes agent queue"
    assert mod.count_completed_goals(world_only=True) == 2, \
        "world_only must skip agent-queue files"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
