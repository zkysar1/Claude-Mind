""" (f): /v1/aspirations/meta-update with values the file already
holds must not rewrite aspirations-meta.json, snapshot its history, or append a
changelog row. Under own-cloud every rewrite is a new store version.

Called in-process with a fake ctx, like test_team_state_write_noop_skip.py.
Each no-write assertion sits beside a write that the same probes do see
(guard-2903: a "nothing changed" check is green by default when broken).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "core" / "scripts"))

OLD = 946684800  # 2000-01-01T00:00:00Z: any rewrite moves the mtime off it


class _FakePaths:
    def __init__(self, world: Path):
        self.world = world
        self.agent = world.parent / "agent"


class _FakeCtx:
    def __init__(self, world: Path, body: dict):
        self.paths = _FakePaths(world)
        self.query = {"source": "world"}
        self.body = json.dumps(body).encode("utf-8")
        self.headers = {"x-mind-agent": "alpha"}


def _setup(tmp_path, monkeypatch, doc_text=None):
    from mind_api.src import history
    from mind_api.src.endpoints import aspirations_write

    world = tmp_path / "world"
    world.mkdir()
    meta = world / "aspirations-meta.json"
    if doc_text is not None:
        meta.write_text(doc_text, encoding="utf-8")
        os.utime(meta, (OLD, OLD))
    snapshots = []
    monkeypatch.setattr(history, "snapshot",
                        lambda *a, **k: snapshots.append(a[0]))
    return aspirations_write, world, meta, snapshots


def _changelog_rows(world: Path) -> int:
    log = world / "changelog.jsonl"
    if not log.exists():
        return 0
    return sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())


def test_repeat_value_writes_nothing_and_a_change_still_writes(tmp_path, monkeypatch):
    # Compact, unsorted text: the endpoint's own dump (indent=2) would differ in
    # bytes, so an unconditional rewrite could not pass the byte check below.
    text = '{"session_count":3,"last_updated":"2026-09-30","readiness_gates":{"b":1,"a":2}}\n'
    aw, world, meta, snapshots = _setup(tmp_path, monkeypatch, text)

    resp = aw.meta_update(_FakeCtx(world, {"last_updated": "2026-09-30"}))
    assert resp.status == 200
    assert json.loads(resp.body)["data"]["last_updated"] == "2026-09-30"
    assert meta.stat().st_mtime == OLD
    assert meta.read_text(encoding="utf-8") == text
    assert snapshots == []
    assert _changelog_rows(world) == 0

    resp = aw.meta_update(_FakeCtx(world, {"session_count": 4}))
    assert resp.status == 200
    assert meta.stat().st_mtime != OLD
    assert json.loads(meta.read_text(encoding="utf-8"))["session_count"] == 4
    assert len(snapshots) == 1
    assert _changelog_rows(world) == 1


def test_a_json_type_change_is_a_change(tmp_path, monkeypatch):
    # Python's == calls true equal to 1; the file must still take the boolean.
    aw, world, meta, snapshots = _setup(tmp_path, monkeypatch, '{"flag": 1}\n')

    aw.meta_update(_FakeCtx(world, {"flag": True}))
    assert meta.stat().st_mtime != OLD
    assert json.loads(meta.read_text(encoding="utf-8"))["flag"] is True
    assert len(snapshots) == 1


def test_a_missing_file_still_materializes(tmp_path, monkeypatch):
    aw, world, meta, snapshots = _setup(tmp_path, monkeypatch)

    aw.meta_update(_FakeCtx(world, {"last_updated": None}))
    assert meta.exists()
    assert json.loads(meta.read_text(encoding="utf-8"))["session_count"] == 0
    assert len(snapshots) == 1
    assert _changelog_rows(world) == 1
