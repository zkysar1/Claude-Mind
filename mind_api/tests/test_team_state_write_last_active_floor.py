""", daemon twin: /v1/team-state/update of a last_active heartbeat
inside the 600 s floor must not rewrite the agent's shard (the predicate and the
CLI twin are pinned in core/scripts/tests/test_team_state_last_active_floor.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))

OLD = 946684800  # 2000-01-01T00:00:00Z: any rewrite moves the mtime off it
OLD_STAMP = "2000-01-01T00:00:00"
T0 = "2026-09-29T20:00:00"


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent_name = "alpha"


class _FakeCtx:
    def __init__(self, world: Path, query: dict):
        self.paths = _FakePaths(world)
        self.query = query
        self.body = b""
        self.headers = {"x-mind-agent": "alpha"}


def _heartbeat(world: Path, value: str):
    from mind_api.src.world import team_state_write
    return team_state_write.update(_FakeCtx(world, {
        "field": "agent_status.alpha.last_active",
        "value": f'"{value}"',
        "operation": "set",
    }))


def test_daemon_heartbeat_inside_the_floor_leaves_the_shard_untouched(tmp_path):
    from _team_state import row_path

    world = tmp_path / "world"
    world.mkdir()
    _heartbeat(world, T0)
    shard = row_path(world, "alpha")
    row = yaml.safe_load(shard.read_text(encoding="utf-8"))
    row["row_updated"] = OLD_STAMP
    shard.write_text(yaml.safe_dump(row, sort_keys=False), encoding="utf-8")
    os.utime(shard, (OLD, OLD))

    _heartbeat(world, "2026-09-29T20:05:00")
    assert shard.stat().st_mtime == OLD
    row = yaml.safe_load(shard.read_text(encoding="utf-8"))
    assert row["last_active"] == T0
    assert row["row_updated"] == OLD_STAMP

    _heartbeat(world, "2026-09-29T20:10:00")
    assert shard.stat().st_mtime != OLD
    row = yaml.safe_load(shard.read_text(encoding="utf-8"))
    assert row["last_active"] == "2026-09-29T20:10:00"
    assert row["row_updated"] != OLD_STAMP
