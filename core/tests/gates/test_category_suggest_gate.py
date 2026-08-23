"""Behavior + equivalence tests for category_suggest gate (PR 7c/4).

Classifier — always returns a list. No would_block, no override. Empty
list on missing tree / no matches.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "category-suggest.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from gates.category_suggest import evaluate  # noqa: E402


@pytest.fixture
def tree_env(tmp_path: Path):
    """Construct a tmp world with a _tree.yaml carrying a handful of nodes."""
    world = tmp_path / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    tree = {
        "nodes": {
            # Depth 0/1 are STRUCTURAL — should never appear in suggestions
            "root": {"summary": "tree root", "depth": 0, "children": []},
            "system": {"summary": "system root", "depth": 1, "children": []},
            # Depth 2+ are eligible
            "api-auth": {
                "summary": "Authentication retry logic, OAuth flows, token handling",
                "depth": 2, "children": [],
            },
            "deployment-cicd": {
                "summary": "CI/CD pipeline configuration and deployment workflows",
                "depth": 2, "children": [],
            },
            "framework-loop": {
                "summary": "The aspirations perpetual loop and goal selection",
                "depth": 2, "children": [],
            },
        },
        "entity_index": {},
    }
    import yaml
    (tree_dir / "_tree.yaml").write_text(
        yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")
    return world


def _run_cli(world: Path, text: str, top: int = 3) -> tuple[int, list]:
    """Invoke the CLI with WORLD_DIR pointed at the tmp tree."""
    import os
    env = os.environ.copy()
    env["MIND_WORLD"] = str(world)
    proc = subprocess.run(
        [sys.executable, str(CLI), "--text", text, "--top", str(top)],
        env=env, capture_output=True, text=True, check=False,
    )
    return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else []


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------

def test_exact_key_match_top_score(tree_env):
    """Text containing 'api-auth' should match api-auth at top."""
    matches = evaluate("Fix the api-auth retry logic",
                       world_dir=tree_env)
    assert matches
    assert matches[0]["key"] == "api-auth"
    # Exact substring + segment overlap → at least 3.0 + something
    assert matches[0]["score"] >= 3.0


def test_summary_overlap_matches(tree_env):
    """No exact key match, but summary words overlap."""
    matches = evaluate("authentication tokens and OAuth issues",
                       world_dir=tree_env)
    assert matches
    assert matches[0]["key"] == "api-auth"


def test_no_match_returns_empty(tree_env):
    """Use text with no 3+-character word overlap on any node key or
    summary. Common 3-letter words ('and', 'the') would partial-match
    summary text and produce a weak (0.5) hit — that's legacy behavior,
    not a bug, so this test deliberately avoids those stopwords."""
    matches = evaluate("xylophones zebras", world_dir=tree_env)
    assert matches == []


def test_structural_depths_excluded(tree_env):
    """Even when text matches a depth-0/1 node, it must not appear."""
    matches = evaluate("system tree root", world_dir=tree_env)
    keys = [m["key"] for m in matches]
    assert "root" not in keys
    assert "system" not in keys


def test_top_n_caps_output(tree_env):
    matches = evaluate("authentication deployment framework",
                       world_dir=tree_env, top_n=2)
    assert len(matches) <= 2


def test_top_n_default_is_3(tree_env):
    """Match text against all 3 eligible nodes; default top_n=3 returns all 3."""
    matches = evaluate("authentication deployment framework",
                       world_dir=tree_env)
    assert len(matches) <= 3


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------

def test_missing_world_dir_returns_empty():
    """No world_dir AND no tree_path → empty list, no crash."""
    assert evaluate("anything") == []


def test_missing_tree_file_returns_empty(tmp_path: Path):
    """world_dir exists but tree file doesn't → empty list."""
    empty_world = tmp_path / "empty-world"
    empty_world.mkdir()
    assert evaluate("anything", world_dir=empty_world) == []


def test_malformed_tree_returns_empty(tmp_path: Path):
    """YAML parses to a non-dict → empty list."""
    world = tmp_path / "bad-world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text("- not a dict\n", encoding="utf-8")
    assert evaluate("anything", world_dir=world) == []


def test_empty_nodes_returns_empty(tmp_path: Path):
    world = tmp_path / "empty-nodes-world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text("nodes: {}\n", encoding="utf-8")
    assert evaluate("anything", world_dir=world) == []


# ---------------------------------------------------------------------------
# tree_path override (lets callers point at non-default tree)
# ---------------------------------------------------------------------------

def test_explicit_tree_path_overrides_world_dir(tree_env, tmp_path: Path):
    """Pointing tree_path at a different tree must win over world_dir's
    default-derived path."""
    other_world = tmp_path / "other"
    other_tree_dir = other_world / "knowledge" / "tree"
    other_tree_dir.mkdir(parents=True)
    import yaml
    other_tree = {"nodes": {
        "shared-cache": {
            "summary": "Shared cache layer for cross-service reads",
            "depth": 2, "children": [],
        },
    }, "entity_index": {}}
    other_tree_path = other_tree_dir / "_tree.yaml"
    other_tree_path.write_text(yaml.safe_dump(other_tree), encoding="utf-8")

    matches = evaluate(
        "shared cache",
        world_dir=tree_env,            # would resolve api-auth/deployment-cicd
        tree_path=other_tree_path,     # overrides — only sees shared-cache
    )
    assert matches
    assert matches[0]["key"] == "shared-cache"


# ---------------------------------------------------------------------------
# CLI vs module equivalence
# ---------------------------------------------------------------------------

def test_cli_module_equivalent(tree_env):
    text = "Fix the api-auth retry logic"
    rc, cli_out = _run_cli(tree_env, text)
    mod_out = evaluate(text, world_dir=tree_env)
    assert rc == 0
    assert cli_out == mod_out


def test_cli_no_match_returns_empty_list(tree_env):
    rc, cli_out = _run_cli(tree_env, "totally unrelated xyzzy")
    assert rc == 0
    assert cli_out == []


# ---------------------------------------------------------------------------
# : node bodies resolve against the world that OWNS the tree, never
# against the import-bound _paths.WORLD_DIR -- None on a daemon started before
# its world existed, agent A's world on a daemon serving agent B. Fourth daemon
# call site of the  class (the other three live in tree.py / the
# retrieve endpoint / retrieve.py). These tests also reference _TREE_CACHE and
# _load_tree_cached, which  asked to see pinned.
# ---------------------------------------------------------------------------

def _entity_world(tmp_path: Path) -> Path:
    """A tmp world whose one eligible node carries an `entities` list in its
    .md front matter. The probe text below shares NO token with the node key
    or summary, so the only channel that can score it is the entity channel
    (+1.5 exact) -- a hit proves the body was actually read."""
    world = tmp_path / "entity-world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "payments-ledger.md").write_text(
        "---\nentities:\n  - acmeledger\n---\n# ledger\n", encoding="utf-8")
    tree = {
        "nodes": {
            "root": {"summary": "tree root", "depth": 0, "children": []},
            "payments-ledger": {
                "summary": "reconciliation of monthly statements",
                "depth": 2, "children": [],
                "file": "world/knowledge/tree/payments-ledger.md",
            },
        },
        "entity_index": {},
    }
    import yaml
    (tree_dir / "_tree.yaml").write_text(
        yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")
    return world


@pytest.fixture
def pre_init_daemon(monkeypatch):
    """Model the daemon that bound _paths.WORLD_DIR = None at import, and
    isolate the module-level tree cache on both sides of the test."""
    import _paths
    from gates import category_suggest as cs
    monkeypatch.setattr(_paths, "WORLD_DIR", None)
    cs._TREE_CACHE.clear()
    yield
    cs._TREE_CACHE.clear()


def test_pre_init_daemon_positive_control_module_global_path_raises(
        tmp_path: Path, pre_init_daemon):
    """Under WORLD_DIR=None the bare tree_match path still raises -- so the
    test below cannot pass vacuously."""
    import yaml
    from tree_match import build_concept_index
    world = _entity_world(tmp_path)
    nodes = yaml.safe_load((world / "knowledge" / "tree" / "_tree.yaml")
                           .read_text(encoding="utf-8"))["nodes"]
    with pytest.raises(RuntimeError, match="WORLD_DIR unresolved"):
        build_concept_index(nodes)


def test_pre_init_daemon_entity_channel_resolves_against_world_dir(
        tmp_path: Path, pre_init_daemon):
    """The daemon's call shape (aspirations_write.py: evaluate(text,
    world_dir=ctx.paths.world)) must neither raise nor lose the entity
    channel when the module global is None."""
    world = _entity_world(tmp_path)
    matches = evaluate("acmeledger outage", world_dir=world)
    assert [m["key"] for m in matches] == ["payments-ledger"]
    assert matches[0]["score"] == 1.5  # entity channel only; no key/summary overlap


def test_explicit_tree_path_resolves_bodies_under_its_own_world(
        tmp_path: Path, pre_init_daemon):
    """An explicit canonical tree_path owns its bodies: the world root is
    derived from the tree's location, not from world_dir (documented unused)."""
    world = _entity_world(tmp_path)
    other = tmp_path / "some-other-world"
    other.mkdir()
    matches = evaluate("acmeledger outage", world_dir=other,
                       tree_path=world / "knowledge" / "tree" / "_tree.yaml")
    assert [m["key"] for m in matches] == ["payments-ledger"]
