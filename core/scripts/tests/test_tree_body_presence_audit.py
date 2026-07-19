"""Targeted tests for tree-body-presence-audit.py.

Covers the two behaviors the goal's verification checks name:
  1. local-backend no-op (never calls stat())
  2. the 4-way local x remote classification (synced / local_only /
     cache_miss / desync) via a fake remote backend.

Fake backends inject stat() results, so no real store is touched. guard-955:
run under STORAGE_BACKEND=local (the conftest autouse pin covers this; the
fake backends make it moot anyway).
"""
import importlib.util
from pathlib import Path

import yaml

_SCRIPT = Path(__file__).resolve().parents[1] / "tree-body-presence-audit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("tree_body_presence_audit", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeStat:
    """Truthy stand-in for a present remote object."""


class _FakeRemoteBackend:
    """Remote-synced backend whose stat() returns None (404) for a configured set."""
    name = "own-cloud"

    def __init__(self, absent_paths):
        self._absent = {str(p) for p in absent_paths}

    def stat(self, path):
        if str(path) in self._absent:
            return None
        return _FakeStat()


class _FakeLocalBackend:
    name = "local"

    def stat(self, path):  # pragma: no cover - must never be reached
        raise AssertionError("stat() must never be called on the local backend (no-op expected)")


def _write_tree(world_dir, nodes):
    """nodes: dict key -> file_field ('world/...' path) or None for a no-file node."""
    tree_dir = world_dir / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    ndict = {}
    for k, ff in nodes.items():
        ndict[k] = {"file": ff} if ff else {"parent": "root"}
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump({"nodes": ndict}))
    return tree_dir


def test_local_backend_is_noop(tmp_path):
    mod = _load_module()
    _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    result = mod.scan(str(tmp_path), str(tmp_path), backend=_FakeLocalBackend())
    assert result["local_noop"] is True
    assert result["backend"] == "local"
    assert "counts" not in result  # early return — never classified, never HEADed


def test_four_way_classification(tmp_path):
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {
        "synced_node": "world/knowledge/tree/synced.md",
        "localonly_node": "world/knowledge/tree/localonly.md",
        "cachemiss_node": "world/knowledge/tree/cachemiss.md",
        "desync_node": "world/knowledge/tree/desync.md",
        "root": None,  # no file -> no_file_nodes
    })
    # Local bodies present for synced + localonly; absent for cachemiss + desync.
    (tree_dir / "synced.md").write_text("x")
    (tree_dir / "localonly.md").write_text("x")
    # Remote returns 404 for localonly (never pushed) + desync (absent everywhere).
    backend = _FakeRemoteBackend(absent_paths=[tree_dir / "localonly.md", tree_dir / "desync.md"])

    result = mod.scan(str(tmp_path), str(tmp_path), backend=backend)

    assert result["local_noop"] is False
    c = result["counts"]
    assert c["synced"] == 1
    assert c["local_only"] == 1
    assert c["cache_miss"] == 1
    assert c["desync"] == 1
    assert c["probe_error"] == 0
    assert result["no_file_nodes"] == 1
    assert result["total_with_file"] == 4
    assert {r["key"] for r in result["desync"]} == {"desync_node"}
    assert {r["key"] for r in result["local_only"]} == {"localonly_node"}


def test_probe_error_bucket(tmp_path):
    """A backend whose stat() raises lands the node in probe_error, not a false desync."""
    mod = _load_module()
    tree_dir = _write_tree(tmp_path, {"n1": "world/knowledge/tree/n1.md"})
    (tree_dir / "n1.md").write_text("x")

    class _RaisingBackend:
        name = "own-cloud"

        def stat(self, path):
            raise RuntimeError("transient HEAD failure")

    result = mod.scan(str(tmp_path), str(tmp_path), backend=_RaisingBackend())
    assert result["counts"]["probe_error"] == 1
    assert result["counts"]["desync"] == 0
    assert result["probe_error"][0]["key"] == "n1"
