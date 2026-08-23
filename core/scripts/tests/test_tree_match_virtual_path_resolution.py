"""Pin that build_concept_index RESOLVES virtual node paths ().

The defect this guards was silent by construction. tree_match joined each
node's VIRTUAL-prefixed `file` ("world/knowledge/tree/...") onto PROJECT_ROOT.
Under .mind-data storage WORLD_DIR is PROJECT_ROOT/.mind-data/world, so
PROJECT_ROOT/world/ does not exist, parse_front_matter's `if not p.exists():
return {}` fired for EVERY node, and the index built from nothing — disabling
Strategy 4, the only token-level natural-language path into the tree.

Nothing failed. An empty concept index and a corpus with no `entities:` at all
produce byte-identical output, so every consumer kept working and kept
returning worse results. It survived 14 days and three independent
re-verifications (bravo 2026-07-25, alpha 2026-07-27, echo 2026-08-08) with
concept_index_terms = 0 each time.

So the assertion that matters is NOT "resolve_file_path is called" — that is a
mock check that passes whether or not the path resolves. It is "a node whose
`file` carries the virtual prefix contributes its entities to the index", plus
a POSITIVE CONTROL (rb-245) proving the fixture actually sits where the pre-fix
join would have missed it. Without that control a fixture that happened to live
under PROJECT_ROOT would pass both before and after the fix, and this file
would be decoration.

Measured on cc-07 2026-08-10 over the live tree: 1364 nodes, PROJECT_ROOT arm
resolved 0/1363 node bodies, resolve_file_path arm resolved 1363/1363, and the
index went 0 -> 2859 terms.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

import _paths  # noqa: E402
import tree_match as tm  # noqa: E402


NODE_MD = """---
topic: Fixture Node
entities:
  - tailnet-ip
  - fleet-topology-roster-fixture
---

# Fixture Node

Body text.
"""


@pytest.fixture
def virtual_world(tmp_path, monkeypatch):
    """A WORLD_DIR that is NOT a child of PROJECT_ROOT/world.

    That separation is the whole point: it reproduces the .mind-data shape
    where the virtual prefix and the real location diverge.
    """
    world = tmp_path / "detached-world"
    node_dir = world / "knowledge" / "tree" / "system"
    node_dir.mkdir(parents=True)
    (node_dir / "fixture-node.md").write_text(NODE_MD, encoding="utf-8")
    # resolve_file_path reads _paths.WORLD_DIR at CALL time, so patching the
    # module global is enough — tree_match's `from _paths import ...` holds a
    # reference to the function, not to the directory.
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setenv("STORAGE_BACKEND", "local")  # guard-955
    return world


NODES = {"fixture-node": {"file": "world/knowledge/tree/system/fixture-node.md"}}


def test_positive_control_prefix_join_misses_this_fixture(virtual_world):
    """If this ever fails, the fixture stopped exercising the defect.

    The pre-fix expression was `PROJECT_ROOT / file_path`. It must NOT resolve
    for this node, or the test below would pass with the bug reintroduced.
    """
    pre_fix = _paths.PROJECT_ROOT / NODES["fixture-node"]["file"]
    assert not pre_fix.exists(), (
        f"fixture no longer reproduces the defect: {pre_fix} exists, so the "
        f"pre-fix PROJECT_ROOT join would have found it and the assertions "
        f"below would pass even with the bug restored"
    )


def test_virtual_prefixed_node_contributes_its_entities(virtual_world):
    """The behaviour, not the call. This is what was broken for 14 days."""
    index = tm.build_concept_index(NODES)
    assert index, (
        "concept index is EMPTY for a node whose .md carries entities — the "
        "virtual `world/` prefix is not being resolved (g-115-3099 / guard-132)"
    )
    assert "tailnet-ip" in index
    assert index["tailnet-ip"] == ["fixture-node"]
    assert "fleet-topology-roster-fixture" in index


def test_front_matter_reads_through_the_resolved_path(virtual_world):
    """parse_front_matter must receive the RESOLVED path, not the virtual one.

    Covers the sibling half of the same defect: the own-cloud ensure_local call
    sits directly above the exists() gate in parse_front_matter and was handed
    the same unresolved path. OwnCloudBackend._refresh no-ops for any path under
    no configured root, so it was INERT rather than mis-keyed — silently, which
    is why it went unnoticed while its own comment described preventing exactly
    this degradation.
    """
    resolved = _paths.resolve_file_path(NODES["fixture-node"]["file"])
    assert resolved.exists(), "fixture path did not resolve under the patched WORLD_DIR"
    fm = tm.parse_front_matter(resolved)
    assert fm.get("entities") == ["tailnet-ip", "fleet-topology-roster-fixture"]


def test_missing_body_still_degrades_quietly(virtual_world):
    """A node whose .md is genuinely absent must contribute nothing, not raise.

    The fix must not convert a tolerable gap into a crash: node records can
    outlive their bodies, and build_concept_index runs over the whole tree.
    """
    nodes = dict(NODES)
    nodes["ghost"] = {"file": "world/knowledge/tree/system/does-not-exist.md"}
    index = tm.build_concept_index(nodes)
    assert "tailnet-ip" in index
    assert all("ghost" not in keys for keys in index.values())


def test_node_without_a_file_field_is_skipped(virtual_world):
    nodes = dict(NODES)
    nodes["fileless"] = {"summary": "no file key at all"}
    index = tm.build_concept_index(nodes)
    assert "tailnet-ip" in index


# ---------------------------------------------------------------------------
# : a daemon that started BEFORE its world was configured has
# _paths.WORLD_DIR = None for the life of the process, while every request
# carries the correct root in ctx.paths.world. build_concept_index must be able
# to take that root explicitly; otherwise the daemon's stale import-time state
# wins and every find-node / retrieve request fails with an error that blames
# the config file -- which is correct on disk. Measured 2026-08-22 on zc-03.
# ---------------------------------------------------------------------------


@pytest.fixture
def pre_init_daemon(virtual_world, monkeypatch):
    """The literal production state: the world EXISTS on disk and is what the
    request would resolve, but the module global bound at import is None."""
    monkeypatch.setattr(_paths, "WORLD_DIR", None)
    return virtual_world


def test_pre_init_daemon_positive_control_module_global_path_fails(pre_init_daemon):
    """Without an explicit root the old path MUST fail here -- this is the
    failure the next test proves is fixed. If this ever passes, the fixture no
    longer reproduces the daemon state and the test below is decoration."""
    with pytest.raises(RuntimeError, match="WORLD_DIR unresolved"):
        tm.build_concept_index(NODES)


def test_pre_init_daemon_explicit_world_root_builds_the_index(pre_init_daemon):
    index = tm.build_concept_index(NODES, world_root=pre_init_daemon)
    assert index.get("tailnet-ip") == ["fixture-node"]
    assert index.get("fleet-topology-roster-fixture") == ["fixture-node"]


def test_explicit_world_root_wins_over_a_stale_module_global(virtual_world, tmp_path, monkeypatch):
    """A STALE (wrong, not None) import-time root must not be consulted when
    the caller supplies one -- the daemon serving agent B with a WORLD_DIR that
    was bound to agent A's world at startup. The stale world is EMPTY, so the
    old code would silently build an empty index (the guard-132 failure shape)
    rather than raise."""
    stale = tmp_path / "some-other-world"
    stale.mkdir()
    monkeypatch.setattr(_paths, "WORLD_DIR", stale)
    index = tm.build_concept_index(NODES, world_root=virtual_world)
    assert index.get("tailnet-ip") == ["fixture-node"]
