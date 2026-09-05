"""test_concept_cache_fingerprint.py — the daemon's concept-index cache is
keyed on a CONTENT fingerprint of the tree nodes, not on id(nodes)
(2026-09-03).

The concept index (entity term -> node keys) is a pure function of each
node's `file` and that file's front matter, and every hook-mediated front
matter edit bumps the node's `last_updated` in _tree.yaml. The old id(nodes)
key missed on every yaml_cache reload, and _tree.yaml reloads on every box
each time ANY agent's counting retrieval writes a retrieval_count into it —
so on a busy fleet essentially every request rebuilt the index from 1,569
files (measured 24.4 s cold vs 4.3 s warm for one request).

Invariants pinned here:
  1. A reload with identical (key, file, last_updated) content HITS even
     though the dict identity changed (the retrieval-counter case).
  2. A changed last_updated, a new key, or a different world_root MISSES.
  3. A malformed nodes value degrades to the old id(nodes) key rather than
     to a stale hit.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "core" / "scripts"))
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

ep = importlib.import_module("mind_api.src.endpoints.retrieve")


def _nodes():
    return {
        "alpha": {"file": "world/knowledge/tree/a/alpha.md", "last_updated": "2026-09-01",
                  "retrieval_count": 3},
        "beta": {"file": "world/knowledge/tree/a/beta.md", "last_updated": "2026-08-30",
                 "retrieval_count": 0},
    }


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    with ep._concept_cache_lock:
        ep._concept_cache.clear()
    calls = []

    def _fake_build(nodes, world_root=None):
        calls.append(str(world_root))
        return {"term": sorted(nodes)}
    monkeypatch.setattr(ep, "_real_build_concept_index", _fake_build)
    yield calls
    with ep._concept_cache_lock:
        ep._concept_cache.clear()


def test_identical_content_hits_across_dict_identity(_fresh_cache):
    n1 = _nodes()
    n2 = copy.deepcopy(n1)
    n2["alpha"]["retrieval_count"] = 99  # a counter bump: not part of the key
    assert n1 is not n2
    a = ep._cached_build_concept_index(n1, world_root="/w")
    b = ep._cached_build_concept_index(n2, world_root="/w")
    assert a == b
    assert len(_fresh_cache) == 1, "second call must be a cache HIT"


def test_last_updated_change_misses(_fresh_cache):
    n1 = _nodes()
    ep._cached_build_concept_index(n1, world_root="/w")
    n2 = copy.deepcopy(n1)
    n2["alpha"]["last_updated"] = "2026-09-03"
    ep._cached_build_concept_index(n2, world_root="/w")
    assert len(_fresh_cache) == 2


def test_new_key_misses(_fresh_cache):
    n1 = _nodes()
    ep._cached_build_concept_index(n1, world_root="/w")
    n2 = copy.deepcopy(n1)
    n2["gamma"] = {"file": "world/knowledge/tree/a/gamma.md", "last_updated": "2026-09-03"}
    ep._cached_build_concept_index(n2, world_root="/w")
    assert len(_fresh_cache) == 2


def test_world_root_is_part_of_the_key(_fresh_cache):
    n1 = _nodes()
    ep._cached_build_concept_index(n1, world_root="/w1")
    ep._cached_build_concept_index(copy.deepcopy(n1), world_root="/w2")
    assert _fresh_cache == ["/w1", "/w2"]


def test_malformed_nodes_degrade_to_identity_key():
    bad = {"k": "not-a-dict"}
    assert ep._concept_fingerprint(bad) == id(bad)
    assert ep._concept_fingerprint(_nodes()) != id(bad)


def test_entries_do_not_pin_the_nodes_dict(_fresh_cache):
    n1 = _nodes()
    ep._cached_build_concept_index(n1, world_root="/w")
    with ep._concept_cache_lock:
        entries = list(ep._concept_cache.values())
    assert entries and all(e[1] is None for e in entries)
