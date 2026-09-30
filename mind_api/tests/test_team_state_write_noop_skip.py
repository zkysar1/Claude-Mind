""", daemon twin: /v1/team-state/update with an unchanged value must
not rewrite the agent's shard (the CLI twin is pinned in
core/scripts/tests/test_team_state_noop_write_skip.py).
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


def test_daemon_repeat_set_leaves_the_shard_untouched(tmp_path):
    from mind_api.src.world import team_state_write
    from _team_state import row_path

    world = tmp_path / "world"
    world.mkdir()
    query = {"field": "agent_status.alpha.box", "value": '"host-a"', "operation": "set"}
    team_state_write.update(_FakeCtx(world, dict(query)))
    shard = row_path(world, "alpha")
    # Back-date the stamp as well as the mtime: row_updated has one-second
    # resolution, so a same-second repeat would re-stamp the identical value
    # and hide a missing stamp-on-change guard.
    row = yaml.safe_load(shard.read_text(encoding="utf-8"))
    row["row_updated"] = "2000-01-01T00:00:00"
    shard.write_text(yaml.safe_dump(row, sort_keys=False), encoding="utf-8")
    os.utime(shard, (OLD, OLD))

    team_state_write.update(_FakeCtx(world, dict(query)))
    assert shard.stat().st_mtime == OLD
    assert yaml.safe_load(shard.read_text(encoding="utf-8"))["row_updated"] == "2000-01-01T00:00:00"

    query["value"] = '"host-b"'
    team_state_write.update(_FakeCtx(world, dict(query)))
    assert shard.stat().st_mtime != OLD
    assert yaml.safe_load(shard.read_text(encoding="utf-8"))["box"] == "host-b"
