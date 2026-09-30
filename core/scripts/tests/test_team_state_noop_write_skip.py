""": a team-state write that changes no field must not rewrite the shard.

Under own-cloud every rewrite is a new object version, and re-writing unchanged
heartbeat fields regrew a 7 KB team-state shard into tens of MB of store
metadata (15 of 39 consecutive versions of one shard carried no field change,
measured 2026-09-29). Two layers fix it: `locked_modify_yaml(skip_if_unchanged=True)`
writes nothing when the modifier returns what it read, and the team-state row
writers stamp `row_updated` only when a field changed.

The primitive tests count calls to the atomic writer (the effect), not the file
contents, because an identical rewrite leaves identical bytes (guard-1965). The
CLI tests age the shard's mtime first, so any rewrite is visible.
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

import _fileops  # noqa: E402
from _team_state import row_path  # noqa: E402

TEAM_STATE_PY = CORE_SCRIPTS / "team-state.py"
OLD = 946684800  # 2000-01-01T00:00:00Z: any rewrite moves the mtime off it


@pytest.fixture
def writes(monkeypatch):
    calls = []
    real = _fileops._atomic_write_with_fallback

    def _counting(path, write_fn, **kw):
        calls.append(Path(path))
        return real(path, write_fn, **kw)

    monkeypatch.setattr(_fileops, "_atomic_write_with_fallback", _counting)
    return calls


def _seed(p: Path, data: dict) -> None:
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_unchanged_result_writes_nothing(tmp_path, writes):
    p = tmp_path / "row.yaml"
    _seed(p, {"box": "host-a", "last_active": "2026-09-29T17:00:00"})
    before = p.read_bytes()

    def _same(row):
        row["box"] = "host-a"
        return row

    _fileops.locked_modify_yaml(p, _same, skip_if_unchanged=True)
    assert writes == []
    assert p.read_bytes() == before


def test_default_still_writes_an_unchanged_result(tmp_path, writes):
    p = tmp_path / "row.yaml"
    _seed(p, {"box": "host-a"})
    _fileops.locked_modify_yaml(p, lambda row: row)
    assert writes == [p]


def test_changed_result_is_written(tmp_path, writes):
    p = tmp_path / "row.yaml"
    _seed(p, {"box": "host-a"})

    def _change(row):
        row["box"] = "host-b"
        return row

    _fileops.locked_modify_yaml(p, _change, skip_if_unchanged=True)
    assert writes == [p]
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"box": "host-b"}


@pytest.mark.parametrize("content", [None, ""], ids=["missing", "empty"])
def test_missing_or_empty_file_still_materializes(tmp_path, writes, content):
    p = tmp_path / "row.yaml"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    _fileops.locked_modify_yaml(p, lambda row: row, initial={"box": "host-a"},
                                skip_if_unchanged=True)
    assert writes == [p]
    assert yaml.safe_load(p.read_text(encoding="utf-8")) == {"box": "host-a"}


def _age(shard: Path) -> None:
    """Back-date the row's stamp AND mtime. row_updated has one-second
    resolution, so a repeat call inside the same second would re-stamp the
    identical value and hide a missing stamp-on-change guard."""
    row = yaml.safe_load(shard.read_text(encoding="utf-8"))
    row["row_updated"] = "2000-01-01T00:00:00"
    shard.write_text(yaml.safe_dump(row, sort_keys=False), encoding="utf-8")
    os.utime(shard, (OLD, OLD))


def _stamp(shard: Path) -> str:
    return yaml.safe_load(shard.read_text(encoding="utf-8"))["row_updated"]


def _run(world: Path, *args: str) -> None:
    env = os.environ.copy()
    env["MIND_AGENT"] = "alpha"
    env["MIND_WORLD"] = str(world)
    env["STORAGE_BACKEND"] = "local"
    r = subprocess.run([sys.executable, str(TEAM_STATE_PY), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr


def test_cli_repeat_set_leaves_the_shard_untouched(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    field = "agent_status.alpha.box"
    _run(world, "update", "--field", field, "--value", '"host-a"')
    shard = row_path(world, "alpha")
    _age(shard)

    _run(world, "update", "--field", field, "--value", '"host-a"')
    assert shard.stat().st_mtime == OLD
    assert _stamp(shard) == "2000-01-01T00:00:00"

    _run(world, "update", "--field", field, "--value", '"host-b"')
    assert shard.stat().st_mtime != OLD
    assert _stamp(shard) != "2000-01-01T00:00:00"
    assert yaml.safe_load(shard.read_text(encoding="utf-8"))["box"] == "host-b"


def test_cli_clear_in_flight_with_nothing_to_clear_leaves_the_shard_untouched(tmp_path):
    world = tmp_path / "world"
    world.mkdir()
    _run(world, "update", "--field", "agent_status.alpha.box", "--value", '"host-a"')
    shard = row_path(world, "alpha")
    _age(shard)

    _run(world, "clear-in-flight", "--agent", "alpha")
    assert shard.stat().st_mtime == OLD
    assert _stamp(shard) == "2000-01-01T00:00:00"
