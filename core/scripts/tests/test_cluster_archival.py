"""test_cluster_archival.py —  (asp-303, D2 Phase 2).

Pins the D2 cluster-archival engine (core/scripts/tree_archive.py):

  PURE (no WORLD_DIR — import the engine directly):
   - compute_cluster edge (i): empty-tags + no-source-asp leaf clusters on
     signal A (shared parent) alone.
   - compute_cluster edge (ii): signal C uses the Jaccard FLOOR, not
     intersection-non-empty (a single ubiquitous shared tag like `framework`
     does NOT cluster; high tag overlap DOES). The mega-cluster regression guard.
   - compute_cluster edge (iii): interior nodes are NEVER cluster members.
   - MAX_CLUSTER_SIZE scope guard: a tag shared by >cap leaves falls back to A∪B.
   - effective_relevance / staleness_days / _is_leaf unit behavior.

  INTEGRATION (subprocess + tmp world, mirrors test_origin_goal_id /
  test_poignancy_producer):
   - scan emits a stale leaf candidate with its cluster + refresh-eligibility.
   - apply archive --force marks node_type=archived + detaches from parent
     (rebuilds child_count); restore re-attaches.
   - apply archive on an INTERIOR node refuses (interior re-check, §5 layer 2).
   - apply archive while DORMANT (archives_per_pass=0) refuses without --force
     (natural-gate, guard-348).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent          # core/scripts/tests
CORE_SCRIPTS = SCRIPT_DIR.parent                       # core/scripts
PROJECT_ROOT = CORE_SCRIPTS.parent.parent              # repo root
for p in (str(CORE_SCRIPTS), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import tree_archive  # noqa: E402  (pure helpers import nothing from tree.py/_paths)

ARCHIVE_PY = CORE_SCRIPTS / "tree_archive.py"
PYTHON = sys.executable


# ---------------------------------------------------------------------------
# PURE — compute_cluster edge cases (the §1 testable contract)
# ---------------------------------------------------------------------------

def test_cluster_archival_edge_i_signal_a_alone():
    """A leaf with empty tags + no source-asp clusters on shared parent only."""
    leaves = {
        "a": {"parent": "P", "child_count": 0},
        "b": {"parent": "P", "child_count": 0},   # sibling under P → signal A
        "c": {"parent": "Q", "child_count": 0},   # different parent, no B/C
    }
    fm_map = {
        "a": {"tags": set(), "asp": None},
        "b": {"tags": {"x"}, "asp": "asp-9"},
        "c": {"tags": {"y"}, "asp": "asp-8"},
    }
    cluster = tree_archive._compute_cluster_pure("a", leaves, fm_map)
    assert cluster == {"a", "b"}


def test_cluster_archival_edge_ii_jaccard_floor_not_intersection():
    """Signal C requires Jaccard >= floor, NOT a non-empty intersection.

    `b` shares only the ubiquitous `framework` tag (Jaccard 1/7 < 0.5) → excluded.
    `d` has full tag overlap (Jaccard 1.0) → included. Regression guard against
    one ubiquitous tag collapsing the tree into a mega-cluster."""
    leaves = {
        "a": {"parent": "P", "child_count": 0},
        "b": {"parent": "Q", "child_count": 0},
        "d": {"parent": "R", "child_count": 0},
    }
    fm_map = {
        "a": {"tags": {"framework", "tree", "archival", "cluster"}, "asp": None},
        "b": {"tags": {"framework", "deploy", "ci", "ops"}, "asp": None},
        "d": {"tags": {"framework", "tree", "archival", "cluster"}, "asp": None},
    }
    cluster = tree_archive._compute_cluster_pure(
        "a", leaves, fm_map, tag_jaccard_floor=0.5, max_cluster_size=40)
    assert cluster == {"a", "d"}     # b excluded (below floor), d included (overlap)


def test_cluster_archival_edge_iii_interior_never_member():
    """Interior nodes (child_count > 0) are excluded from cluster membership."""
    nodes = {
        "P": {"child_count": 2, "children": ["a", "b"]},   # interior
        "a": {"parent": "P", "child_count": 0},
        "b": {"parent": "P", "child_count": 0},
    }
    leaves = {k: v for k, v in nodes.items() if tree_archive._is_leaf(v)}
    fm_map = {k: {"tags": set(), "asp": None} for k in leaves}
    cluster = tree_archive._compute_cluster_pure("a", leaves, fm_map)
    assert "P" not in cluster
    assert cluster == {"a", "b"}


def test_cluster_archival_max_cluster_size_fallback():
    """A tag shared by more than MAX_CLUSTER_SIZE leaves falls back to A∪B."""
    leaves = {f"n{i}": {"parent": f"P{i}", "child_count": 0} for i in range(50)}
    fm_map = {f"n{i}": {"tags": {"framework"}, "asp": None} for i in range(50)}
    cluster = tree_archive._compute_cluster_pure(
        "n0", leaves, fm_map, tag_jaccard_floor=0.5, max_cluster_size=40)
    # C would cluster all 50 (>40 cap) → fall back to A∪B; n0's parent P0 is
    # unique and asp is None → only n0 remains.
    assert cluster == {"n0"}


def test_cluster_archival_signal_b_shared_aspiration():
    """Signal B clusters leaves sharing source_aspiration across parents."""
    leaves = {
        "a": {"parent": "P", "child_count": 0},
        "b": {"parent": "Q", "child_count": 0},
    }
    fm_map = {
        "a": {"tags": set(), "asp": "asp-303"},
        "b": {"tags": set(), "asp": "asp-303"},
    }
    assert tree_archive._compute_cluster_pure("a", leaves, fm_map) == {"a", "b"}


def test_cluster_archival_relevance_and_leaf_helpers():
    """effective_relevance prefers the most recent of last_retrieved /
    (last_relevant_at or last_updated); _is_leaf keys off child_count."""
    from datetime import date
    today = date(2026, 6, 14)
    # last_relevant_at falls back to last_updated when null; last_retrieved wins
    # when more recent.
    n1 = {"last_updated": "2025-01-01", "last_relevant_at": None,
          "last_retrieved": "2026-06-10"}
    assert tree_archive.staleness_days(n1, today) == 4
    n2 = {"last_updated": "2025-01-01", "last_relevant_at": "2026-06-01"}
    assert tree_archive.staleness_days(n2, today) == 13
    n3 = {}  # no signal → default-to-keep (None, never eligible)
    assert tree_archive.staleness_days(n3, today) is None
    assert tree_archive._is_leaf({"child_count": 0}) is True
    assert tree_archive._is_leaf({"child_count": 3}) is False
    assert tree_archive._is_leaf({"children": []}) is True
    assert tree_archive._is_leaf({"children": ["x"]}) is False


# ---------------------------------------------------------------------------
# INTEGRATION — subprocess + tmp world
# ---------------------------------------------------------------------------

def _seed_tree(world: Path):
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {"nodes": {
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "parent": None, "children": ["stale-leaf", "fresh-leaf"],
                 "child_count": 2, "capability_level": "EXPLOIT"},
        "stale-leaf": {"file": "world/knowledge/tree/stale-leaf.md", "depth": 1,
                       "parent": "root", "children": [], "child_count": 0,
                       "summary": "a stale leaf", "last_updated": "2025-01-01"},
        "fresh-leaf": {"file": "world/knowledge/tree/fresh-leaf.md", "depth": 1,
                       "parent": "root", "children": [], "child_count": 0,
                       "summary": "a fresh sibling", "last_updated": "2025-01-01",
                       "last_retrieved": "2026-06-10"},
        # An isolated subtree so the lonely leaf has no live cluster member.
        "root2": {"file": "world/knowledge/tree/root2.md", "depth": 0,
                  "parent": None, "children": ["lonely-leaf"],
                  "child_count": 1, "capability_level": "EXPLOIT"},
        "lonely-leaf": {"file": "world/knowledge/tree/lonely-leaf.md", "depth": 1,
                        "parent": "root2", "children": [], "child_count": 0,
                        "summary": "a lonely stale leaf", "last_updated": "2025-01-01"},
    }}
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump(tree), encoding="utf-8")
    return tree_dir / "_tree.yaml"


def _run(args, world: Path, meta: Path):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env.pop("MIND_AGENT", None)
    return subprocess.run([PYTHON, str(ARCHIVE_PY)] + args,
                          text=True, capture_output=True, env=env, timeout=60)


def _load(tree_path):
    return yaml.safe_load(tree_path.read_text(encoding="utf-8"))


def test_cluster_archival_scan_emits_stale_candidate(tmp_path):
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world)
    r = _run(["scan", "--today", "2026-06-14"], world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout)
    keys = {c["key"] for c in out["candidates"]}
    # stale-leaf and lonely-leaf are both >180d stale; fresh-leaf is not.
    assert "stale-leaf" in keys
    assert "lonely-leaf" in keys
    assert "fresh-leaf" not in keys
    stale = next(c for c in out["candidates"] if c["key"] == "stale-leaf")
    # cluster picks up fresh-leaf (shared parent root → signal A), which was
    # retrieved within 60d → refresh-eligible.
    assert "fresh-leaf" in stale["cluster"]
    assert stale["refresh_eligible"] is True
    assert out["dormant"] is True   # archives_per_pass=0 in the real config


def test_cluster_archival_apply_archive_and_restore(tmp_path):
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    # --force bypasses the dormancy gate (manual/test archive).
    r = _run(["apply", "lonely-leaf", "archive", "--force", "--today", "2026-06-14"],
             world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["detached_from"] == "root2"
    tree = _load(tree_path)
    assert tree["nodes"]["lonely-leaf"]["node_type"] == "archived"
    assert tree["nodes"]["root2"]["children"] == []      # detached
    assert tree["nodes"]["root2"]["child_count"] == 0
    assert out["parent_emptied_flag"] is True             # §5 corollary flag

    # restore re-attaches and clears archived markers.
    r2 = _run(["restore", "lonely-leaf"], world, meta)
    assert r2.returncode == 0, f"stderr={r2.stderr}"
    tree2 = _load(tree_path)
    assert tree2["nodes"]["lonely-leaf"]["node_type"] == "leaf"
    assert "lonely-leaf" in tree2["nodes"]["root2"]["children"]
    assert tree2["nodes"]["root2"]["child_count"] == 1


def test_cluster_archival_apply_archive_interior_refused(tmp_path):
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world)
    r = _run(["apply", "root", "archive", "--force", "--today", "2026-06-14"],
             world, meta)
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["error"] == "interior_node_refused"


def test_cluster_archival_apply_archive_dormant_refused(tmp_path):
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world)
    # No --force: the natural gate (archives_per_pass=0) refuses.
    r = _run(["apply", "lonely-leaf", "archive", "--today", "2026-06-14"],
             world, meta)
    assert r.returncode == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["error"] == "dormant"
