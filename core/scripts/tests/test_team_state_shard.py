"""test_team_state_shard.py —  per-agent team-state sharding.

world/team-state.yaml carried every agent's agent_status row — the hottest
cross-writer file in the fleet (every agent bumps last_active/in_flight every
iteration; on own-cloud each write CAS-contends on ONE S3 object). The shard
moves each agent's row to world/team-state/agents/<name>.yaml — single-writer
by construction — while shared fields stay in the core file. Reads compose
the two (rows win newest-wins over core residuals).

Covers:
  - route_field routing table (unit)
  - CLI update / in-flight / clear-in-flight land in the ROW file; the core
    file is not touched by row-scoped writes; core fields still land core
  - composed read: union, per-agent newest-wins in BOTH directions,
    last_updated lifted to the newest row stamp
  - lazy migration: first row write self-seeds from the core residual
  - migrate-shard: one-shot cleanup, idempotent, never rolls a live row back
  - goal-verification criterion (g-328-27): 5 parallel agents bumping
    last_active — all land, zero lock/CAS failures, merged read correct

CLI exercised via subprocess against an ISOLATED tmp world (MIND_WORLD
override — never touches the live team-state). The daemon mirror routes
through the same _team_state helpers (guard-742 parity by construction).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import yaml  # noqa: E402

from _team_state import (  # noqa: E402
    compose_agent_status,
    compose_state,
    core_residual,
    load_rows,
    read_agent_row,
    route_field,
    row_agent_names,
    row_path,
    rows_dir,
)

TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"


def _run(world: Path, agent: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MIND_AGENT"] = agent
    env["MIND_WORLD"] = str(world)
    cmd = [sys.executable, str(TEAM_STATE_PY), *args]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=60)


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


# --- unit: routing -----------------------------------------------------------

def test_route_field_table():
    assert route_field("agent_status.alpha.last_active") == ("row", "alpha", "last_active")
    assert route_field("agent_status.alpha.in_flight.phase") == ("row", "alpha", "in_flight.phase")
    assert route_field("agent_status.alpha") == ("row", "alpha", "")
    # Bare map + shared fields stay core.
    assert route_field("agent_status")[0] == "core"
    assert route_field("strategic_focus.primary")[0] == "core"
    assert route_field("critical_blockers")[0] == "core"
    assert route_field("shared_cadences.slot-x")[0] == "core"


def test_row_path_rejects_hostile_names():
    with tempfile.TemporaryDirectory() as tmpd:
        for bad in ("", ".", "..", "a/b", "a\\b"):
            try:
                row_path(Path(tmpd), bad)
                raise AssertionError(f"row_path accepted {bad!r}")
            except ValueError:
                pass


# --- CLI routing: rows vs core ----------------------------------------------

def test_row_write_lands_in_row_file_not_core():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run(world, "alpha", "update",
                 "--field", "agent_status.alpha.last_active",
                 "--value", '"2026-07-07T10:00:00"')
        assert r.returncode == 0, r.stderr
        row = _load(row_path(world, "alpha"))
        assert row["last_active"] == "2026-07-07T10:00:00"
        assert row["row_updated_by"] == "alpha"
        # Core file is NOT created by a row-scoped write.
        assert not (world / "team-state.yaml").exists()


def test_core_field_still_lands_in_core():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run(world, "alpha", "update",
                 "--field", "strategic_focus.primary", "--value", '"stability"')
        assert r.returncode == 0, r.stderr
        core = _load(world / "team-state.yaml")
        assert core["strategic_focus"]["primary"] == "stability"
        assert core["agent_status"] == {}
        assert not rows_dir(world).exists() or not list(rows_dir(world).iterdir())


def test_whole_row_set_routes_to_row():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run(world, "echo", "update",
                 "--field", "agent_status.echo",
                 "--value", '{"last_active":"2026-07-07T11:00:00","current_focus":"session ended"}')
        assert r.returncode == 0, r.stderr
        row = _load(row_path(world, "echo"))
        assert row["current_focus"] == "session ended"
        assert row["row_updated_by"] == "echo"


def test_in_flight_and_clear_roundtrip_on_row():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        r = _run(world, "delta", "in-flight", "--agent", "delta",
                 "--goal-id", "g-328-27", "--title", "shard", "--phase", "4")
        assert r.returncode == 0, r.stderr
        row = _load(row_path(world, "delta"))
        assert row["in_flight"]["goal_id"] == "g-328-27"
        assert row["current_focus"] == "asp-328: shard"
        r = _run(world, "delta", "clear-in-flight", "--agent", "delta")
        assert r.returncode == 0, r.stderr
        assert "cleared" in r.stdout
        row = _load(row_path(world, "delta"))
        assert "in_flight" not in row
        assert row["last_active"]  # liveness bump preserved


# --- composition --------------------------------------------------------------

def test_compose_union_and_newest_wins_both_directions():
    core = {
        "stale": {"last_active": "2026-07-01T00:00:00", "current_focus": "old"},
        "fresh_core": {"last_active": "2026-07-07T12:00:00", "current_focus": "core-truth"},
        "core_only": {"last_active": "2026-07-02T00:00:00"},
    }
    rows = {
        "stale": {"last_active": "2026-07-07T09:00:00", "current_focus": "row-truth"},
        "fresh_core": {"last_active": "2026-07-03T00:00:00", "current_focus": "row-stale"},
        "row_only": {"last_active": "2026-07-04T00:00:00"},
    }
    out = compose_agent_status(core, rows)
    assert sorted(out) == ["core_only", "fresh_core", "row_only", "stale"]
    assert out["stale"]["current_focus"] == "row-truth"        # row newer wins
    assert out["fresh_core"]["current_focus"] == "core-truth"  # core newer wins (rollback window)
    # Whole-snapshot side-pick, never field-stitch.
    assert out["stale"] is rows["stale"]


def test_composed_read_lifts_last_updated():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        _run(world, "alpha", "update",
             "--field", "strategic_focus.primary", "--value", '"x"')
        _run(world, "bravo", "update",
             "--field", "agent_status.bravo.last_active",
             "--value", '"2099-01-01T00:00:00"')
        state = _load(world / "team-state.yaml")
        composed = compose_state(dict(state), world)
        assert composed["agent_status"]["bravo"]["last_active"] == "2099-01-01T00:00:00"
        assert composed["last_updated"] == "2099-01-01T00:00:00"
        assert composed["last_updated_by"] == "bravo"


def test_read_agent_row_prefers_newer_side():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core_path = world / "team-state.yaml"
        core_path.write_text(yaml.safe_dump({
            "agent_status": {"zeta": {"last_active": "2026-07-07T12:00:00",
                                      "current_focus": "core-newer"}}}),
            encoding="utf-8")
        rows_dir(world).mkdir(parents=True)
        row_path(world, "zeta").write_text(yaml.safe_dump(
            {"last_active": "2026-07-01T00:00:00", "current_focus": "row-older"}),
            encoding="utf-8")
        got = read_agent_row(world, "zeta", core_path=core_path)
        assert got["current_focus"] == "core-newer"
        # And row-newer direction:
        row_path(world, "zeta").write_text(yaml.safe_dump(
            {"last_active": "2026-07-08T00:00:00", "current_focus": "row-newer"}),
            encoding="utf-8")
        got = read_agent_row(world, "zeta", core_path=core_path)
        assert got["current_focus"] == "row-newer"


# --- lazy migration + one-shot cleanup ----------------------------------------

def test_first_row_write_self_seeds_from_core_residual():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        (world / "team-state.yaml").write_text(yaml.safe_dump({
            "agent_status": {"zeta": {
                "in_flight": {"goal_id": "g-old-1", "title": "t",
                              "claimed_at": "2026-07-01T00:00:00", "phase": "4"},
                "last_active": "2026-07-01T00:00:00",
                "custom_field": "survives-seed",
            }}}), encoding="utf-8")
        # First row write is a clear-in-flight: the residual must seed the row
        # so the clear actually clears in the composed view.
        r = _run(world, "zeta", "clear-in-flight", "--agent", "zeta")
        assert r.returncode == 0, r.stderr
        assert "cleared" in r.stdout, r.stdout  # seeded in_flight was popped
        row = _load(row_path(world, "zeta"))
        assert "in_flight" not in row
        assert row["custom_field"] == "survives-seed"
        composed = compose_state(_load(world / "team-state.yaml"), world)
        assert "in_flight" not in composed["agent_status"]["zeta"]


def test_migrate_shard_moves_residuals_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        (world / "team-state.yaml").write_text(yaml.safe_dump({
            "strategic_focus": {"primary": "keep-me"},
            "agent_status": {
                "alpha": {"last_active": "2026-07-01T00:00:00"},
                "bravo": {"last_active": "2026-07-02T00:00:00"},
            }}), encoding="utf-8")
        before = compose_state(_load(world / "team-state.yaml"), world)
        r = _run(world, "alpha", "migrate-shard")
        assert r.returncode == 0, r.stderr
        assert "moved 2 row(s)" in r.stdout, r.stdout
        core = _load(world / "team-state.yaml")
        assert core["agent_status"] == {}
        assert core["strategic_focus"]["primary"] == "keep-me"
        assert set(row_agent_names(world)) == {"alpha", "bravo"}
        after = compose_state(core, world)
        for name in ("alpha", "bravo"):
            assert (after["agent_status"][name]["last_active"]
                    == before["agent_status"][name]["last_active"])
        # Re-run: nothing to move; live rows never rolled back.
        row_path(world, "alpha").write_text(yaml.safe_dump(
            {"last_active": "2026-07-09T00:00:00"}), encoding="utf-8")
        r = _run(world, "alpha", "migrate-shard")
        assert r.returncode == 0, r.stderr
        assert "nothing to move" in r.stdout
        assert _load(row_path(world, "alpha"))["last_active"] == "2026-07-09T00:00:00"


def test_core_residual_helper_reads_one_agent():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        core = world / "team-state.yaml"
        assert core_residual(core, "alpha") == {}  # missing file → empty seed
        core.write_text(yaml.safe_dump({
            "agent_status": {"alpha": {"last_active": "2026-07-05T00:00:00"}}}),
            encoding="utf-8")
        assert core_residual(core, "alpha")["last_active"] == "2026-07-05T00:00:00"
        assert core_residual(core, "ghost") == {}


def test_load_rows_skips_corrupt_row_loudly():
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)
        rows_dir(world).mkdir(parents=True)
        row_path(world, "good").write_text(
            yaml.safe_dump({"last_active": "2026-07-07T00:00:00"}), encoding="utf-8")
        row_path(world, "bad").write_text("{unclosed: [", encoding="utf-8")
        rows = load_rows(world)
        assert "good" in rows and "bad" not in rows


# --- goal verification: parallel 5-agent bumps --------------------------------

def test_parallel_five_agent_bumps_all_land_zero_conflicts():
    """ verification criterion: parallel last_active bumps from 5
    simulated agents — all land, zero lock-acquisition failures, merged read
    correct. Different agents write DIFFERENT files, so contention is
    structurally impossible; this pins the property end-to-end via the CLI."""
    agents = [f"shard-{i}" for i in range(5)]
    with tempfile.TemporaryDirectory() as tmpd:
        world = Path(tmpd)

        def bump(agent: str) -> subprocess.CompletedProcess:
            return _run(world, agent, "update",
                        "--field", f"agent_status.{agent}.last_active",
                        "--value", f'"2026-07-07T13:00:0{agent[-1]}"')

        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(bump, agents))
        for a, r in zip(agents, results):
            assert r.returncode == 0, f"{a}: rc={r.returncode} stderr={r.stderr}"
            assert "Could not acquire lock" not in (r.stderr or ""), r.stderr
        composed = compose_state({}, world)
        assert sorted(composed["agent_status"]) == sorted(agents)
        for i, a in enumerate(agents):
            assert (composed["agent_status"][a]["last_active"]
                    == f"2026-07-07T13:00:0{i}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("ALL OK")
