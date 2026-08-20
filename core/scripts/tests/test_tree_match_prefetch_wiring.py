"""Pin that build_concept_index PREFETCHES before its per-node walk ().

`parse_front_matter` calls `get_backend().ensure_local(p)` per node, so a tree
walk pays one HEAD per node — 1436 HEADs / 135.8s on the live cc-02 tree. One
bulk listing in front of the loop makes that 3 LIST + 1 HEAD / 1.7s with a
BYTE-IDENTICAL index (measured 2026-08-19, zeta, hostname cc-02, uname -r
6.8.0-137-generic, 1437 nodes).

WHY THIS TEST IS AT THE CALLER LAYER. `test_owncloud_prefetch.py` already
covers the primitive, and it passes whether or not anything calls it — a
capability with no call site is indistinguishable from one never built, which
is the defect g-115-6671 exists to close. So the assertions here are about the
WIRING: that prefetch is called, once, with a root the node bodies actually sit
under.

THE SPELLING ASSERTION IS THE LOAD-BEARING ONE. The head-skip half of the
optimization is keyed by RAW PATH SPELLING — `prefetch` warms
`_cache_check[str(root / rel)]` while `_refresh` reads
`_cache_check[str(self._local(path))]`, and `_local` is identity, so neither
side normalizes. A prefetch root spelled differently from the read paths warms
entries nothing looks up: every HEAD is still paid, `stats["warmed"]` still
reports success, and no test that only checks "prefetch was called" can tell.
`test_prefetch_root_contains_the_node_bodies` is what discriminates.
"""

import importlib
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # noqa: E402
import storage_backend  # noqa: E402
import tree_match as tm  # noqa: E402


NODE_MD = """---
topic: Prefetch Fixture
entities:
  - prefetch-wiring-fixture
---

Body.
"""


class RecordingBackend:
    """Records the ORDER of prefetch vs ensure_local — the wiring is only a win
    if the listing lands BEFORE the per-file reads it is meant to replace."""

    def __init__(self, prefetch_raises=False):
        self.calls = []
        self.prefetch_raises = prefetch_raises

    def prefetch(self, path):
        self.calls.append(("prefetch", Path(path)))
        if self.prefetch_raises:
            raise RuntimeError("simulated listing failure")
        return {"backend": "recording", "listed": 0, "warmed": 0}

    def ensure_local(self, path):
        self.calls.append(("ensure_local", Path(path)))
        return Path(path)

    def prefetch_calls(self):
        return [p for kind, p in self.calls if kind == "prefetch"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = tmp_path / "detached-world"
    node_dir = w / "knowledge" / "tree" / "system"
    node_dir.mkdir(parents=True)
    for i in range(tm._PREFETCH_MIN_NODES + 5):
        (node_dir / f"node-{i}.md").write_text(NODE_MD, encoding="utf-8")
    monkeypatch.setattr(_paths, "WORLD_DIR", w)
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955
    return w


def _nodes(n):
    return {
        f"node-{i}": {"file": f"world/knowledge/tree/system/node-{i}.md"}
        for i in range(n)
    }


@pytest.fixture
def backend(monkeypatch):
    b = RecordingBackend()
    monkeypatch.setattr(storage_backend, "get_backend", lambda: b)
    return b


def test_full_tree_walk_prefetches_exactly_once(world, backend):
    """The wiring. Once per build — not once per node, not zero times."""
    tm.build_concept_index(_nodes(tm._PREFETCH_MIN_NODES + 5))
    assert len(backend.prefetch_calls()) == 1, (
        f"expected exactly one bulk listing per index build, got "
        f"{len(backend.prefetch_calls())} — a per-node prefetch would be far "
        f"worse than the HEADs it replaces"
    )


def test_prefetch_lands_before_the_first_per_node_read(world, backend):
    """A listing after the walk warms a cache nobody will look up again."""
    tm.build_concept_index(_nodes(tm._PREFETCH_MIN_NODES + 5))
    kinds = [k for k, _ in backend.calls]
    assert "prefetch" in kinds and "ensure_local" in kinds
    assert kinds.index("prefetch") < kinds.index("ensure_local")


def test_prefetch_root_contains_the_node_bodies(world, backend):
    """THE discriminator: prefetch's root and the read paths must agree.

    `_cache_check` is keyed by raw spelling on both sides with no
    normalization, so a root the node bodies do not resolve under warms
    entries that are never read — silently, at full cost, while every other
    assertion in this file still passes.
    """
    tm.build_concept_index(_nodes(tm._PREFETCH_MIN_NODES + 5))
    root = backend.prefetch_calls()[0]
    read_paths = [p for k, p in backend.calls if k == "ensure_local"]
    assert read_paths, "no per-node reads happened — fixture is not exercising the walk"
    for p in read_paths:
        assert str(p).startswith(str(root) + "/") or str(p).startswith(str(root) + "\\"), (
            f"node body {p} is not under the prefetch root {root} — the "
            f"head-skip is keyed by raw spelling, so this warms nothing and "
            f"every HEAD is still paid (g-115-6671 addendum)"
        )


def test_small_node_set_does_not_pay_a_listing(world, backend):
    """Below the threshold the listing costs more than the HEADs it saves."""
    tm.build_concept_index(_nodes(tm._PREFETCH_MIN_NODES - 1))
    assert backend.prefetch_calls() == [], (
        f"a {tm._PREFETCH_MIN_NODES - 1}-node build must not pay a full-tree "
        f"listing (break-even is ~10 nodes; see _prefetch_tree_root)"
    )


def test_index_is_unchanged_when_the_listing_fails(world, monkeypatch):
    """Fail-open: this is an optimization, never a correctness dependency."""
    ok = RecordingBackend()
    monkeypatch.setattr(storage_backend, "get_backend", lambda: ok)
    expected = tm.build_concept_index(_nodes(tm._PREFETCH_MIN_NODES + 5))
    assert expected, "fixture produced an empty index — nothing is being compared"

    broken = RecordingBackend(prefetch_raises=True)
    monkeypatch.setattr(storage_backend, "get_backend", lambda: broken)
    got = tm.build_concept_index(_nodes(tm._PREFETCH_MIN_NODES + 5))
    assert got == expected


def test_local_backend_prefetch_is_a_real_no_op():
    """The default backend must not pay a filesystem sweep for this wiring."""
    importlib.reload(storage_backend)
    lb = storage_backend.LocalBackend()
    stats = lb.prefetch(Path("."))
    assert stats["listed"] == 0 and stats["warmed"] == 0
