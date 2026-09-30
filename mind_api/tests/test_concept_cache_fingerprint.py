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
  4. (g-115-11505) T21 stamps last_updated as a DATE, so a second edit on the
     same day leaves it unmoved. The file mtime of every node dated within a
     day of the tree's newest date is in the key: such an edit MISSES, an
     older node's file change does not re-key (the cost bound), and the
     window follows the tree's dates, capped at today so a future-dated
     typo cannot move it.
"""
from __future__ import annotations

import copy
import os
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


# --- : same-day edits ------------------------------------------

def _write_node(root, rel, entities):
    p = root / "knowledge" / "tree" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = ", ".join(f'"{e}"' for e in entities)
    p.write_text(f"---\nentities: [{body}]\n---\n\nbody\n", encoding="utf-8")
    return p


def _bump_mtime(p):
    # Explicit, so a coarse-mtime filesystem cannot hide the rewrite.
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def _dated(recent, old="2026-01-01"):
    return {
        "alpha": {"file": "world/knowledge/tree/a/alpha.md", "last_updated": recent},
        "old": {"file": "world/knowledge/tree/a/old.md", "last_updated": old},
    }


def _same_day_edit(tmp_path, nodes):
    alpha = _write_node(tmp_path, "a/alpha.md", ["crdf"])
    _write_node(tmp_path, "a/old.md", ["x"])
    ep._cached_build_concept_index(nodes, world_root=tmp_path)
    _write_node(tmp_path, "a/alpha.md", ["crdf", "vipre"])
    _bump_mtime(alpha)
    ep._cached_build_concept_index(copy.deepcopy(nodes), world_root=tmp_path)


def test_unedited_recent_node_still_hits(_fresh_cache, tmp_path):
    _write_node(tmp_path, "a/alpha.md", ["crdf"])
    _write_node(tmp_path, "a/old.md", ["x"])
    n1 = _dated("2026-09-29")
    ep._cached_build_concept_index(n1, world_root=tmp_path)
    ep._cached_build_concept_index(copy.deepcopy(n1), world_root=tmp_path)
    assert len(_fresh_cache) == 1


def test_same_day_entity_edit_misses(_fresh_cache, tmp_path):
    """The measured 2026-09-29 case: entities added to a node already dated
    today, last_updated unmoved. The rebuild must happen."""
    _same_day_edit(tmp_path, _dated("2026-09-29"))
    assert len(_fresh_cache) == 2


def test_old_node_file_change_does_not_rekey(_fresh_cache, tmp_path):
    """Cost bound: only nodes within a day of the newest date are stat'ed
    (about 30 ms vs 261 ms for all 1,686 on DESKTOP-O91DLK2). An edit to an older
    node moves its date through T21, which the date part of the key sees."""
    _write_node(tmp_path, "a/alpha.md", ["crdf"])
    old = _write_node(tmp_path, "a/old.md", ["x"])
    n1 = _dated("2026-09-29")
    ep._cached_build_concept_index(n1, world_root=tmp_path)
    _bump_mtime(old)
    ep._cached_build_concept_index(copy.deepcopy(n1), world_root=tmp_path)
    assert len(_fresh_cache) == 1


def test_window_follows_the_trees_dates_not_the_clock(_fresh_cache, tmp_path):
    """Dated years before today: a clock-based window would stat nothing."""
    _same_day_edit(tmp_path, _dated("2020-05-05", old="2019-01-01"))
    assert len(_fresh_cache) == 2


def test_malformed_date_does_not_switch_the_window_off(_fresh_cache, tmp_path):
    """'unknown' sorts above every ISO date; it must not become the newest."""
    n1 = _dated("2026-09-29")
    n1["junk"] = {"file": "world/knowledge/tree/a/junk.md", "last_updated": "unknown"}
    _same_day_edit(tmp_path, n1)
    assert len(_fresh_cache) == 2


def test_future_dated_typo_does_not_move_the_window(_fresh_cache, tmp_path):
    """A node stamped 2099 must not become the anchor; the window caps at
    today, so a same-day edit to a node dated today still misses."""
    import datetime as dt
    n1 = _dated(dt.date.today().isoformat())
    n1["typo"] = {"file": "world/knowledge/tree/a/typo.md", "last_updated": "2099-01-01"}
    _same_day_edit(tmp_path, n1)
    assert len(_fresh_cache) == 2


def test_mutation_date_only_key_serves_the_edit_stale(_fresh_cache, tmp_path,
                                                      monkeypatch):
    """Teeth (rb-8706): under the pre- date-only key the same
    scenario is a HIT, the stale index. If this fails, the scenario moved some
    other part of the key and test_same_day_entity_edit_misses proves nothing."""
    monkeypatch.setattr(ep, "_concept_fingerprint", lambda nodes, world_root=None: hash(
        tuple(sorted((str(k), str(n.get("file", "")), str(n.get("last_updated", "")))
                     for k, n in nodes.items()))))
    _same_day_edit(tmp_path, _dated("2026-09-29"))
    assert len(_fresh_cache) == 1
