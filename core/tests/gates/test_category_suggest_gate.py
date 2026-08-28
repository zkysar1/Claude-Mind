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


# ---------------------------------------------------------------------------
# : pin the _TREE_CACHE invalidation semantics added by .
# The cache is keyed on (path, st_mtime_ns, st_size, world_root). The risk the
# goal names is silent and directional: if the mtime keying is wrong, evaluate()
# serves STALE nodes after every /tree write and the only symptom is subtly
# wrong goal categories -- no error, no red test.
#
# Note the fixtures above only .clear() the cache for isolation; clearing a
# cache is not pinning it. These three tests assert the behaviour itself.
# ---------------------------------------------------------------------------

def _cache_tree_world(tmp_path: Path, node_key: str, summary: str) -> Path:
    """A tmp world whose tree carries exactly one eligible node."""
    world = tmp_path / "cache-world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    _rewrite_cache_tree(world, node_key, summary)
    return world


def _rewrite_cache_tree(world: Path, node_key: str, summary: str) -> Path:
    """(Re)write the world's _tree.yaml with one eligible node, then push its
    mtime forward. The explicit utime bump is load-bearing: callers below use
    SAME-LENGTH node keys so st_size is identical across revisions, which is
    what forces the assertion onto st_mtime_ns -- the field whose failure the
    goal describes. Without the bump a coarse filesystem clock could leave
    mtime_ns unchanged and the test would pass for the wrong reason.

    THE BUMP MUST BE MONOTONIC ACROSS REVISIONS, not relative to the mtime this
    call just wrote. Measured 2026-08-28: the original form read the FRESH stat
    and added 1s, so revision 1 (written at T0) landed at T0+1s and revision 2
    (written at T1, milliseconds later) landed at T1+1s -- and whenever T0 and
    T1 fell in the SAME filesystem mtime tick those are the SAME nanosecond.
    st_size is identical by construction here, so an equal mtime makes the whole
    cache key `(path, st_mtime_ns, st_size, world_root)` identical, the cache
    hits, stale nodes are served, and the test fails asserting exactly the
    invalidation bug it was written to disprove. 3 of 6 runs failed; all three
    cache tests were affected. Anchoring on max(previous, fresh) makes each
    revision strictly newer than the last regardless of clock granularity.

    Note this flakiness was invisible to the mutation proofs these tests already
    passed: killing a mutant shows the assertion has teeth, not that the fixture
    is deterministic. They are separate properties and need separate checks."""
    import os
    import yaml
    tree_path = world / "knowledge" / "tree" / "_tree.yaml"
    prev_ns = tree_path.stat().st_mtime_ns if tree_path.exists() else 0
    tree = {
        "nodes": {
            "root": {"summary": "tree root", "depth": 0, "children": []},
            node_key: {"summary": summary, "depth": 2, "children": []},
        },
        "entity_index": {},
    }
    tree_path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")
    st = tree_path.stat()
    next_ns = max(st.st_mtime_ns, prev_ns) + 10**9
    os.utime(tree_path, ns=(next_ns, next_ns))
    return tree_path


@pytest.fixture
def cache_isolated():
    """Isolate the module-level tree cache on both sides of a test."""
    from gates import category_suggest as cs
    cs._TREE_CACHE.clear()
    yield cs
    cs._TREE_CACHE.clear()


def test_tree_cache_serves_fresh_nodes_after_the_tree_file_changes(
        tmp_path: Path, cache_isolated):
    """(1) A tree write must be reflected on the next evaluate().

    The two node keys are deliberately the SAME LENGTH, so the rewritten file
    has an identical st_size and only st_mtime_ns distinguishes the revisions.
    A cache keyed on size alone would serve the stale node here."""
    probe = "monthly statement reconciliation"
    world = _cache_tree_world(tmp_path, "payments-ledger", probe)
    assert [m["key"] for m in evaluate(probe, world_dir=world)] == ["payments-ledger"]

    before = (world / "knowledge" / "tree" / "_tree.yaml").stat().st_size
    _rewrite_cache_tree(world, "billing-runbook", probe)
    after = (world / "knowledge" / "tree" / "_tree.yaml").stat().st_size
    assert before == after, (
        "fixture no longer exercises mtime keying: the two revisions differ in "
        "size, so this test would pass on a size-only cache key")

    assert [m["key"] for m in evaluate(probe, world_dir=world)] == ["billing-runbook"], (
        "stale nodes served after a tree write — _TREE_CACHE invalidation is broken")


def test_tree_cache_reuses_the_parsed_tree_when_the_file_is_unchanged(
        tmp_path: Path, monkeypatch, cache_isolated):
    """(2) Two calls over an UNCHANGED file must not rebuild the concept index.

    Counting build_concept_index is what makes this non-vacuous: asserting only
    that both calls return the same ANSWER would pass even with the cache
    disabled entirely."""
    cs = cache_isolated
    calls = {"n": 0}
    real = cs.build_concept_index

    def counting(nodes, world_root=None):
        calls["n"] += 1
        return real(nodes, world_root=world_root)

    monkeypatch.setattr(cs, "build_concept_index", counting)

    probe = "monthly statement reconciliation"
    world = _cache_tree_world(tmp_path, "payments-ledger", probe)

    first = evaluate(probe, world_dir=world)
    assert calls["n"] == 1, "positive control: a cold call must build the index once"

    second = evaluate(probe, world_dir=world)
    assert calls["n"] == 1, (
        "concept index rebuilt on an unchanged file — the cache is not being hit")
    assert [m["key"] for m in first] == [m["key"] for m in second]

    # Positive control on the other side: a real change MUST rebuild, so the
    # counter above is measuring cache hits and not a dead monkeypatch.
    _rewrite_cache_tree(world, "billing-runbook", probe)
    evaluate(probe, world_dir=world)
    assert calls["n"] == 2, "a changed file must rebuild the concept index"


def test_tree_cache_bound_keeps_the_current_revision(
        tmp_path: Path, cache_isolated):
    """(3) The len>=3 bound must never leave the CURRENT revision uncached.

    _load_tree_cached clears the whole dict at the bound and then inserts, so
    the revision that triggered the eviction is the one retained. Pin that: the
    failure mode of a naive eviction is dropping the entry you are about to
    serve, which would make every subsequent call a miss."""
    cs = cache_isolated
    probe = "monthly statement reconciliation"
    world = _cache_tree_world(tmp_path, "revision-aaaaaa", probe)

    for key in ("revision-bbbbbb", "revision-cccccc", "revision-dddddd"):
        _rewrite_cache_tree(world, key, probe)
        evaluate(probe, world_dir=world)

    assert len(cs._TREE_CACHE) >= 1, "cache empty after the bound fired"
    assert len(cs._TREE_CACHE) <= 3, "cache grew past its stated 3-entry bound"

    tree_path = world / "knowledge" / "tree" / "_tree.yaml"
    st = tree_path.stat()
    current_key = (str(tree_path), st.st_mtime_ns, st.st_size, str(world))
    assert current_key in cs._TREE_CACHE, (
        "the current revision was evicted by the cache bound — every later "
        "call would miss and re-parse")

    assert [m["key"] for m in evaluate(probe, world_dir=world)] == ["revision-dddddd"]
