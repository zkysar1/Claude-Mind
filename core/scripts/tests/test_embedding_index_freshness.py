"""Unit tests for embedding-index-freshness.py () — the per-box
staleness tick wired into iteration-close.sh productivity-check.

Hermetic: the module is importlib-loaded (hyphenated filename), the index dir
is redirected via EMBED_FRESHNESS_INDEX_DIR, spawn is short-circuited with
EMBED_FRESHNESS_DRYRUN=1, and _blend_enabled/_source_mtime are monkeypatched
— no YAML read, no world access, no subprocess."""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent

_spec = importlib.util.spec_from_file_location(
    "embedding_index_freshness", CORE_SCRIPTS / "embedding-index-freshness.py")
fresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fresh)


@pytest.fixture()
def tick_env(tmp_path, monkeypatch, capsys):
    """Standard harness: dry-run mode + redirected index dir with a meta.json
    whose mtime we control. Returns a runner returning (rc, stdout_lines)."""
    idx = tmp_path / "index"
    idx.mkdir()
    monkeypatch.setenv("EMBED_FRESHNESS_DRYRUN", "1")
    monkeypatch.setenv("EMBED_FRESHNESS_INDEX_DIR", str(idx))

    def run():
        rc = fresh.main()
        out = capsys.readouterr().out.strip()
        return rc, ([json.loads(l) for l in out.splitlines()] if out else [])

    return idx, run


def _make_index(idx, age_seconds):
    meta = idx / "meta.json"
    meta.write_text("{}", encoding="utf-8")
    t = time.time() - age_seconds
    os.utime(meta, (t, t))


def test_flag_off_is_silent_noop(tick_env, monkeypatch):
    idx, run = tick_env
    _make_index(idx, age_seconds=3600)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: False)
    monkeypatch.setattr(fresh, "_source_mtime",
                        lambda: pytest.fail("must not probe sources when off"))
    rc, out = run()
    assert rc == 0 and out == []


def test_missing_index_never_spawns_initial_build(tick_env, monkeypatch):
    idx, run = tick_env
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time())
    rc, out = run()
    assert rc == 0 and out == []
    assert not (idx / ".last-update-attempt").exists()


def test_fresh_index_is_silent(tick_env, monkeypatch):
    idx, run = tick_env
    _make_index(idx, age_seconds=0)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time() - 7200)
    rc, out = run()
    assert rc == 0 and out == []


def test_stale_index_fires_and_records_attempt(tick_env, monkeypatch):
    idx, run = tick_env
    _make_index(idx, age_seconds=7200)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time())
    rc, out = run()
    assert rc == 0
    assert out and out[0]["would_spawn"] is True
    assert (idx / ".last-update-attempt").exists()


def test_debounce_blocks_second_fire_in_window(tick_env, monkeypatch):
    idx, run = tick_env
    _make_index(idx, age_seconds=7200)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time())
    rc1, out1 = run()
    assert out1 and out1[0]["would_spawn"] is True
    rc2, out2 = run()  # marker just written — inside the 6h window
    assert rc2 == 0 and out2 == []


def test_expired_debounce_allows_refire(tick_env, monkeypatch):
    idx, run = tick_env
    _make_index(idx, age_seconds=7200)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: time.time())
    marker = idx / ".last-update-attempt"
    marker.write_text("old", encoding="utf-8")
    t = time.time() - fresh.DEBOUNCE_SECONDS - 60
    os.utime(marker, (t, t))
    rc, out = run()
    assert out and out[0]["would_spawn"] is True


def test_unreadable_sources_fail_quiet(tick_env, monkeypatch):
    idx, run = tick_env
    _make_index(idx, age_seconds=7200)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    monkeypatch.setattr(fresh, "_source_mtime", lambda: None)
    rc, out = run()
    assert rc == 0 and out == []


# ── The watch-set itself () ────────────────────────────────────────
# Every test ABOVE monkeypatches _source_mtime away, so none of them ever
# executed it — which is precisely where the defect lived: the corpus
# (embedding-index-build.load_corpus) indexes guardrails + rb + TREE NODES,
# while the watch-set covered only the first two. A tree-only encoding
# therefore never marked the index stale. These tests run the real function
# against a controllable world so the two sets cannot drift apart again.

import _paths  # noqa: E402  (core/scripts is on sys.path via the loader above)


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def _sources(world):
    return [world / "reasoning-bank.jsonl",
            world / "guardrails.jsonl",
            world / "knowledge" / "tree" / "_tree.yaml",
            world / "knowledge" / "tree" / "sub" / "node-a.md"]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """Minimal world carrying all four corpus sources, bound to WORLD_DIR.

    _source_mtime does `from _paths import WORLD_DIR` at CALL time, so patching
    the module attribute is what the function actually reads."""
    w = tmp_path / "world"
    (w / "knowledge" / "tree" / "sub").mkdir(parents=True)
    for p in _sources(w):
        p.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(_paths, "WORLD_DIR", str(w))
    return w


def test_source_mtime_tracks_tree_index_file(world):
    """A new node or a summary edit moves _tree.yaml — the tick must see it."""
    for p in _sources(world):
        _age(p, 3600)
    tree_yaml = world / "knowledge" / "tree" / "_tree.yaml"
    _age(tree_yaml, 0)
    assert fresh._source_mtime() == tree_yaml.stat().st_mtime


def test_source_mtime_tracks_tree_node_bodies(world):
    """A body edit moves ONLY the .md — watching _tree.yaml alone misses it.

    The embedded surface is humanized-key + summary + first body paragraph
    (build.tree_doc_text), so a body-only edit genuinely changes what should
    be indexed."""
    for p in _sources(world):
        _age(p, 3600)
    node = world / "knowledge" / "tree" / "sub" / "node-a.md"
    _age(node, 0)
    assert fresh._source_mtime() == node.stat().st_mtime


def test_source_mtime_still_tracks_supplementary_stores(world):
    """No regression: the rb/guardrails half of the watch-set still fires."""
    for p in _sources(world):
        _age(p, 3600)
    guards = world / "guardrails.jsonl"
    _age(guards, 0)
    assert fresh._source_mtime() == guards.stat().st_mtime


def test_source_mtime_survives_absent_tree(tmp_path, monkeypatch):
    """A world with no knowledge/tree still returns the supplementary mtime —
    the tree probe is additive, never a new failure mode."""
    w = tmp_path / "world"
    w.mkdir()
    rb = w / "reasoning-bank.jsonl"
    rb.write_text("x\n", encoding="utf-8")
    (w / "guardrails.jsonl").write_text("x\n", encoding="utf-8")
    _age(w / "guardrails.jsonl", 3600)
    monkeypatch.setattr(_paths, "WORLD_DIR", str(w))
    assert fresh._source_mtime() == rb.stat().st_mtime


def test_tree_only_change_fires_the_tick(tick_env, world, monkeypatch):
    """ALLOW case, end-to-end: a tree edit newer than the index spawns an
    update even though both supplementary stores are older than it."""
    idx, run = tick_env
    _make_index(idx, age_seconds=3600)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    for p in _sources(world):
        _age(p, 7200)
    _age(world / "knowledge" / "tree" / "sub" / "node-a.md", 0)
    rc, out = run()
    assert rc == 0
    assert out and out[0]["would_spawn"] is True


def test_all_sources_older_than_index_stays_silent(tick_env, world, monkeypatch):
    """REFUSE case, end-to-end: widening the watch-set must not make the tick
    fire unconditionally — an index newer than every source stays quiet."""
    idx, run = tick_env
    _make_index(idx, age_seconds=3600)
    monkeypatch.setattr(fresh, "_blend_enabled", lambda: True)
    for p in _sources(world):
        _age(p, 7200)
    rc, out = run()
    assert rc == 0 and out == []
