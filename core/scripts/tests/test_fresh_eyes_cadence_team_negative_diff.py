"""Unit tests for the 1 TEAM-layer negative-diff self-heal in
fresh-eyes-cadence-check.py.

Sibling of the g-115-1936 per-agent guard (test_fresh_eyes_cadence_negative_diff.py).
A DOWNWARD world-count correction (census double-count repair) leaves the
shared_cadences.<slot> stamp ABOVE the live world count. Only a FIRE re-stamps
the shared stamp (fresh-eyes-record-tick.sh), and the team gate's noop prevents
the fire — so team_aware rituals (fe-tree 200, fe-program 100) starve until the
world count regrows past the stale stamp. The guard re-stamps the shared
cadence to current_world via the daemon (_rt.rt_call POST /v1/team-state/update)
and noops once.

Coverage:
  - negative team_diff → rc=1, one daemon re-stamp write (count=current_world,
    rebaselined_from=old, fired_by/timestamp preserved)
  - daemon write failure → rc=1 fail-open, stderr warning, no crash
  - 0 <= team_diff < cadence → existing noop path, NO daemon write
  - team_diff >= cadence → fire (rc=0), NO daemon write (guard scoped to <0)

Tests use monkeypatch (config, counts, wm, team stamp, _rt.rt_call,
subprocess.run) so no live state or daemon is touched.
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
    spec = importlib.util.spec_from_file_location(
        "fresh_eyes_cadence_check_team_negdiff",
        str(SCRIPT_DIR / "fresh-eyes-cadence-check.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_cadence_module()


def _stub_config(goal_cadence=25):
    return {
        "fresh_eyes_review": {
            "goal_cadence": goal_cadence,
            "wm_slot": "last_fresh_eyes_review",
            "team_aware": True,
        }
    }


def _wire(monkeypatch, mod, *, current, current_world, slot_value, team_stamp):
    monkeypatch.setattr(mod, "_load_yaml", lambda _p: _stub_config())
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
    monkeypatch.setattr(mod, "team_stamp_value", lambda _slot: team_stamp)
    monkeypatch.setattr(sys, "argv", ["fresh-eyes-cadence-check.py"])


def _stub_rt(monkeypatch, mod, *, raise_error=False):
    calls = []

    def fake_rt_call(method, path, query=None, body=None, headers=None):
        if raise_error:
            raise mod._rt.RtError("daemon unreachable (test)")
        calls.append({"method": method, "path": path, "query": query})
        return "{}"

    monkeypatch.setattr(mod._rt, "rt_call", fake_rt_call)
    return calls


# Per-agent slot: last=70, current=100 → diff=30 >= 25, reaches the team gate.
_SLOT = {"timestamp": "2026-06-01T00:00:00", "goals_count_at_last_fire": 70}


def test_negative_team_diff_rebaselines_and_noops(mod, monkeypatch, capsys):
    """team stamp 900 > current_world 600 → re-stamp to 600, rc=1."""
    _wire(
        monkeypatch, mod,
        current=100, current_world=600,
        slot_value=_SLOT,
        team_stamp={
            "timestamp": "2026-07-01T12:00:00",
            "world_goals_count_at_last_fire": 900,
            "fired_by": "bravo",
        },
    )
    calls = _stub_rt(monkeypatch, mod)
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, f"expected noop (rc=1), got rc={rc}; out={out!r}"
    assert "re-baselined shared_cadences" in out
    assert len(calls) == 1, f"expected exactly one daemon re-stamp, got {len(calls)}"
    c = calls[0]
    assert c["method"] == "POST" and c["path"] == "/v1/team-state/update"
    assert c["query"]["field"] == "shared_cadences.last_fresh_eyes_review"
    assert c["query"]["operation"] == "set"
    stamp = json.loads(c["query"]["value"])
    assert stamp["world_goals_count_at_last_fire"] == 600
    assert stamp["rebaselined_from"] == 900
    assert stamp["fired_by"] == "bravo", "fired_by must be preserved"
    assert stamp["timestamp"] == "2026-07-01T12:00:00", "last fire timestamp must be preserved"


def test_negative_team_diff_write_failure_noops(mod, monkeypatch, capsys):
    """Daemon write raising RtError → fail-open noop (rc=1), stderr warning."""
    _wire(
        monkeypatch, mod,
        current=100, current_world=600,
        slot_value=_SLOT,
        team_stamp={"world_goals_count_at_last_fire": 900},
    )
    _stub_rt(monkeypatch, mod, raise_error=True)
    rc = mod.main()
    captured = capsys.readouterr()
    assert rc == 1, f"expected noop (rc=1), got rc={rc}"
    assert "team-stamp re-baseline" in captured.err


def test_team_diff_positive_below_cadence_takes_existing_noop(mod, monkeypatch, capsys):
    """0 <= team_diff=15 < 25 → 8 noop path, NO daemon write."""
    _wire(
        monkeypatch, mod,
        current=100, current_world=80,
        slot_value=_SLOT,
        team_stamp={"world_goals_count_at_last_fire": 65, "fired_by": "bravo"},
    )
    calls = _stub_rt(monkeypatch, mod)
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 1, f"expected team-aware noop (rc=1), got rc={rc}; out={out!r}"
    assert "re-baselined shared_cadences" not in out
    assert calls == [], "guard must not write on non-negative team_diff"


def test_team_diff_stale_fires_without_rebaseline(mod, monkeypatch, capsys):
    """team_diff=40 >= 25 → fire (rc=0), NO daemon write."""
    _wire(
        monkeypatch, mod,
        current=100, current_world=100,
        slot_value=_SLOT,
        team_stamp={"world_goals_count_at_last_fire": 60, "fired_by": "bravo"},
    )
    calls = _stub_rt(monkeypatch, mod)
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, f"expected fire (rc=0), got rc={rc}; out={out!r}"
    assert calls == [], "guard must not write on the fire path"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
