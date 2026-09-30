""": a last_active heartbeat inside the 600 s floor must not rewrite the shard.

Under own-cloud every shard rewrite is a new store version. On echo's shard the
last_active-only writes were 150 of 557 versions on 09-28 and 166 of 582 on
09-29; a 600 s floor drops 92 and 112 of them. The floor is one shared predicate
(`_team_state.last_active_write_is_floored`) that both row writers call before
they touch the row, so a floored heartbeat returns the row as read and
`locked_modify_yaml(skip_if_unchanged=True)` writes nothing.

The CLI tests age the shard's mtime and row_updated first, so any rewrite is
visible. The daemon twin is pinned in
mind_api/tests/test_team_state_write_last_active_floor.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import yaml  # noqa: E402

from _team_state import (  # noqa: E402
    LAST_ACTIVE_WRITE_FLOOR_S,
    last_active_write_is_floored,
    row_path,
)

TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"
OLD = 946684800  # 2000-01-01T00:00:00Z: any rewrite moves the mtime off it
OLD_STAMP = "2000-01-01T00:00:00"
T0 = "2026-09-29T20:00:00"


def _row(last_active=T0):
    return {"box": "host-a", "last_active": last_active}


def test_floor_is_the_hook_cadence_and_far_below_the_hour_readers():
    assert LAST_ACTIVE_WRITE_FLOOR_S == 600


@pytest.mark.parametrize("new, floored", [
    (T0, True),                          # same second
    ("2026-09-29T20:09:59", True),       # 599 s
    ("2026-09-29T20:10:00", False),      # exactly the floor
    ("2026-09-29T21:00:00", False),      # an hour on
    ("2026-09-29T19:59:00", False),      # older than stored: clock skew writes as before
    ("2026-09-29T20:05:00.123456", True),  # fractional seconds parse
])
def test_predicate_by_age(new, floored):
    assert last_active_write_is_floored(_row(), "last_active", "set", new) is floored


@pytest.mark.parametrize("row, subpath, operation, value", [
    (_row(), "box", "set", "host-b"),                        # any other field
    (_row(), "session_started", "set", "2026-09-29T20:05:00"),  # a timestamp in another field
    (_row(), "in_flight.claimed_at", "set", "2026-09-29T20:05:00"),
    (_row(), "", "set", {"last_active": T0}),                # whole-row set
    (_row(), "last_active", "append", T0),                   # not a set
    (_row(None), "last_active", "set", T0),                  # nothing stored
    (_row("not-a-timestamp"), "last_active", "set", T0),     # stored unparseable
    (_row(), "last_active", "set", None),                    # new unparseable
    ("not-a-row", "last_active", "set", T0),                 # row not a mapping
])
def test_predicate_never_floors_other_shapes(row, subpath, operation, value):
    assert last_active_write_is_floored(row, subpath, operation, value) is False


def _run(world: Path, *args: str) -> None:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(world)
    env["STORAGE_BACKEND"] = "local"
    r = subprocess.run([sys.executable, str(TEAM_STATE_PY), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr


def _set_last_active(world: Path, value: str) -> None:
    _run(world, "update", "--field", "agent_status.alpha.last_active",
         "--value", f'"{value}"')


def _age(shard: Path) -> None:
    """Back-date row_updated AND the mtime, so a rewrite shows in both."""
    row = yaml.safe_load(shard.read_text(encoding="utf-8"))
    row["row_updated"] = OLD_STAMP
    shard.write_text(yaml.safe_dump(row, sort_keys=False), encoding="utf-8")
    os.utime(shard, (OLD, OLD))


def _read(shard: Path) -> dict:
    return yaml.safe_load(shard.read_text(encoding="utf-8"))


@pytest.fixture
def world(tmp_path):
    w = tmp_path / "world"
    w.mkdir()
    _set_last_active(w, T0)
    _age(row_path(w, "alpha"))
    return w


def test_cli_heartbeat_inside_the_floor_leaves_the_shard_untouched(world):
    shard = row_path(world, "alpha")
    _set_last_active(world, "2026-09-29T20:05:00")
    assert shard.stat().st_mtime == OLD
    assert _read(shard)["last_active"] == T0
    assert _read(shard)["row_updated"] == OLD_STAMP


def test_cli_heartbeat_at_the_floor_is_written(world):
    shard = row_path(world, "alpha")
    _set_last_active(world, "2026-09-29T20:10:00")
    assert shard.stat().st_mtime != OLD
    assert _read(shard)["last_active"] == "2026-09-29T20:10:00"
    assert _read(shard)["row_updated"] != OLD_STAMP


def test_cli_other_field_inside_the_window_is_still_written(world):
    shard = row_path(world, "alpha")
    _run(world, "update", "--field", "agent_status.alpha.live_phase",
         "--value", '"phase-4"')
    assert shard.stat().st_mtime != OLD
    assert _read(shard)["live_phase"] == "phase-4"
    assert _read(shard)["last_active"] == T0


def test_cli_other_timestamp_field_inside_the_window_is_still_written(world):
    shard = row_path(world, "alpha")
    _run(world, "update", "--field", "agent_status.alpha.session_started",
         "--value", '"2026-09-29T20:05:00"')
    assert shard.stat().st_mtime != OLD
    assert _read(shard)["session_started"] == "2026-09-29T20:05:00"


def test_cli_stored_stamp_ahead_of_the_writer_is_still_overwritten(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    _set_last_active(world, "2026-09-29T21:00:00")
    shard = row_path(world, "alpha")
    _age(shard)
    _set_last_active(world, "2026-09-29T20:30:00")
    assert shard.stat().st_mtime != OLD
    assert _read(shard)["last_active"] == "2026-09-29T20:30:00"
