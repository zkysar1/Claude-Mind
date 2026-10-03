"""Lane tests for the knowledge-tree retrieval spool as `retrieve.load_tree_nodes`
drives it (g-358-231).

test_tree_retrieval_spool.py pins the module. These pin the SEAM that the flag
turns on: what a non-read-only retrieval does to the index and to the spool under
each flag / interval state. Before g-358-231 nothing exercised that seam at all
(the flag defaulted OFF and no test set it), so the lane the fleet flip enables
had no behavioural test.

Same importlib-against-scratch-world bootstrap as test_embedding_tree_channel.py;
the index is a per-test tmp file whose basename is not the real one.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="tree-spool-lane-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location(
    "retrieve_spool_lane_mod", CORE_SCRIPTS / "retrieve.py")
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _tree_retrieval_spool as trs  # noqa: E402

QUERY = ["orchestrating graceful shutdown sequencing"]  # token-matches NODE only
NODE = "shutdown-steps"


@pytest.fixture(autouse=True)
def _lane_env(monkeypatch):
    """The box's live flag must not decide what a hermetic test observes."""
    monkeypatch.delenv(trs.SPOOLED_ENV, raising=False)
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_tree_channel_enabled"] = False
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


@pytest.fixture()
def tree(tmp_path):
    """A real minimal tree: an index plus node .md files; TREE_PATH points at it."""
    tree_root = tmp_path / "knowledge" / "tree"
    nodes = {
        "runner-leases": {
            "file": str(tree_root / "system" / "runner-leases.md"),
            "summary": "cross machine runner lease expiry semantics",
            "depth": 2, "confidence": 0.8,
        },
        NODE: {
            "file": str(tree_root / "system" / (NODE + ".md")),
            "summary": "graceful shutdown sequencing steps",
            "depth": 2, "confidence": 0.8,
        },
    }
    for n in nodes.values():
        p = Path(n["file"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\n---\nbody\n", encoding="utf-8")
    idx = tmp_path / "idx.yaml"
    idx.write_text(yaml.safe_dump({"nodes": nodes, "entity_index": {}}),
                   encoding="utf-8")
    saved = _retrieve.TREE_PATH
    _retrieve.TREE_PATH = idx
    yield idx
    _retrieve.TREE_PATH = saved


def _nodes(idx):
    return yaml.safe_load(idx.read_text(encoding="utf-8"))["nodes"]


def _count(idx, key=NODE):
    return _nodes(idx)[key].get("retrieval_count", 0)


def _rows(idx):
    p = trs.spool_path(idx)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _drained_just_now(idx):
    assert trs.commit_flush(idx, "2026-10-02T00:00:00")


def _drained_long_ago(idx):
    _drained_just_now(idx)
    old = time.time() - trs.DRAIN_INTERVAL_SECONDS - 60
    os.utime(trs.stamp_path(idx), (old, old))


def _retrieve_once(read_only=False):
    results, _channels = _retrieve.load_tree_nodes(QUERY, "medium",
                                                   read_only=read_only)
    assert NODE in {r["key"] for r in results}, "the query must still match"
    return results


def test_flag_off_bumps_the_index_in_request_and_spools_nothing(tree):
    _retrieve_once()
    assert _count(tree) == 1
    assert _nodes(tree)[NODE]["last_retrieved"] == _retrieve.today_str()
    assert not trs.spool_path(tree).exists()


def test_flag_on_inside_the_interval_spools_and_leaves_the_index_bytes_alone(
        tree, monkeypatch):
    monkeypatch.setenv(trs.SPOOLED_ENV, "1")
    _drained_just_now(tree)
    before = tree.read_bytes()
    _retrieve_once()
    assert tree.read_bytes() == before, "the whole-index write is what the lane removes"
    rows = _rows(tree)
    assert [(r["key"], r["delta"]) for r in rows] == [(NODE, 1)]
    assert rows[0]["ts"] == _retrieve.today_str()


def test_the_first_retrieval_on_a_box_that_never_drained_lands_in_the_index(
        tree, monkeypatch):
    monkeypatch.setenv(trs.SPOOLED_ENV, "1")
    _retrieve_once()
    assert _count(tree) == 1
    assert not trs.spool_path(tree).exists()
    assert trs.stamp_path(tree).exists(), "the first drain starts the interval clock"


def test_flag_on_drains_the_whole_spool_when_the_interval_has_elapsed(
        tree, monkeypatch):
    monkeypatch.setenv(trs.SPOOLED_ENV, "1")
    _drained_long_ago(tree)
    for _ in range(4):
        assert trs.record_bump(tree, NODE, "2026-10-01")
    _retrieve_once()
    assert _count(tree) == 5, "4 spooled + this retrieval, folded in one write"
    assert not trs.spool_path(tree).exists() and not trs.flushing_path(tree).exists()


def test_a_key_the_spool_cannot_append_still_lands_through_the_index_write(
        tree, monkeypatch):
    monkeypatch.setenv(trs.SPOOLED_ENV, "1")
    _drained_just_now(tree)
    monkeypatch.setattr(trs, "record_bump", lambda *a, **k: False)
    _retrieve_once()
    assert _count(tree) == 1, "a lost spool append must fall back, never drop the count"


def test_switching_the_flag_off_still_lands_what_was_spooled(tree):
    """Rollback: the flag is off now, but the box spooled while it was on."""
    _drained_long_ago(tree)
    for _ in range(3):
        assert trs.record_bump(tree, NODE, "2026-10-01")
    _retrieve_once()
    assert _count(tree) == 4, "3 spooled + this retrieval, nothing stranded"
    assert not trs.spool_path(tree).exists()


def test_a_read_only_retrieval_touches_neither_the_index_nor_the_spool(
        tree, monkeypatch):
    monkeypatch.setenv(trs.SPOOLED_ENV, "1")
    _drained_long_ago(tree)
    assert trs.record_bump(tree, NODE, "2026-10-01")
    before = tree.read_bytes()
    _retrieve_once(read_only=True)
    assert tree.read_bytes() == before
    assert [(r["key"], r["delta"]) for r in _rows(tree)] == [(NODE, 1)], (
        "a read-only retrieval must not drain, even a due spool")
