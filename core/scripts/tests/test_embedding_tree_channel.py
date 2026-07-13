"""Unit tests for the tree-lane embedding channel ().

Covers retrieve._tree_doc_id_for / _tree_embedding_scores, the eligibility
hook in load_tree_nodes, and the emb-vs-TF-IDF bonus swap in
_score_weight_limit. Same importlib-against-scratch-world bootstrap as
test_embedding_blend.py; the tree is a per-test tmp _tree.yaml with real
(minimal) node .md files so build_concept_index walks something real.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="embedding-tree-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_treech_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _embedding_retrieval as er  # noqa: E402


def _cfg(tree_on):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_tree_channel_enabled"] = tree_on
    return cfg


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


# ── _tree_doc_id_for: the -trap join key ─────────────────────────────

def test_doc_id_from_posix_and_windows_paths():
    n1 = {"file": "world/knowledge/tree/system/loop/pacing.md"}
    n2 = {"file": "world\\knowledge\\tree\\system\\loop\\pacing.md"}
    assert _retrieve._tree_doc_id_for(n1) == "tree:system/loop/pacing"
    assert _retrieve._tree_doc_id_for(n2) == "tree:system/loop/pacing"


def test_doc_id_none_for_non_tree_or_missing_file():
    assert _retrieve._tree_doc_id_for({"file": "somewhere/else.md"}) is None
    assert _retrieve._tree_doc_id_for({}) is None
    assert _retrieve._tree_doc_id_for(None) is None


# ── _tree_embedding_scores ───────────────────────────────────────────────────

NODES = {
    "pacing": {"file": "world/knowledge/tree/system/loop/pacing.md",
               "summary": "loop pacing", "depth": 3},
    "colors": {"file": "world/knowledge/tree/art/colors.md",
               "summary": "palette", "depth": 2},
}


def test_scores_flag_off_never_calls_cosine(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)

    def _boom(*a, **k):
        raise AssertionError("cosine_scores must not be called when flag off")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    assert _retrieve._tree_embedding_scores(["q"], NODES) == {}


def test_scores_join_tree_ids_back_to_basenames(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    raw = {"tree:system/loop/pacing": 0.9, "tree:art/colors": 0.2,
           "rb-123": 0.99}  # supplementary rows must be ignored by the join
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: raw)
    out = _retrieve._tree_embedding_scores(["q"], NODES)
    assert out == {"pacing": pytest.approx(0.9), "colors": pytest.approx(0.2)}


def test_scores_degrade_to_empty_on_error(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)

    def _boom(*a, **k):
        raise RuntimeError("index corrupt")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    assert _retrieve._tree_embedding_scores(["q"], NODES) == {}


# ── load_tree_nodes end-to-end with a tmp tree ───────────────────────────────

@pytest.fixture()
def tmp_tree(tmp_path):
    """A real minimal tree: _tree.yaml + node .md files under
    <tmp>/knowledge/tree/, with retrieve's TREE_PATH pointed at it."""
    import yaml
    tree_root = tmp_path / "knowledge" / "tree"
    nodes = {
        "runner-leases": {
            "file": str(tree_root / "system" / "runner-leases.md"),
            "summary": "cross machine runner lease expiry semantics",
            "depth": 2, "confidence": 0.8,
        },
        "shutdown-steps": {
            "file": str(tree_root / "system" / "shutdown-steps.md"),
            "summary": "graceful shutdown sequencing steps",
            "depth": 2, "confidence": 0.8,
        },
    }
    for n in nodes.values():
        p = Path(n["file"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\n---\nbody\n", encoding="utf-8")
    tree_yaml = tmp_path / "_tree.yaml"
    tree_yaml.write_text(yaml.safe_dump({"nodes": nodes, "entity_index": {}}),
                         encoding="utf-8")
    saved = _retrieve.TREE_PATH
    _retrieve.TREE_PATH = tree_yaml
    yield nodes
    _retrieve.TREE_PATH = saved


QUERY = ["orchestrating graceful shutdown sequencing"]  # token-matches shutdown-steps only


def test_load_tree_nodes_flag_off_is_token_only(tmp_tree, monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)

    def _boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    results, channels = _retrieve.load_tree_nodes(QUERY, "medium", read_only=True)
    keys = {r["key"] for r in results}
    assert "shutdown-steps" in keys
    assert "runner-leases" not in keys  # zero token overlap, no semantic lane


def test_load_tree_nodes_embedding_channel_widens_and_ranks(tmp_tree, monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    raw = {"tree:system/runner-leases": 0.9, "tree:system/shutdown-steps": 0.5}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: raw)
    results, channels = _retrieve.load_tree_nodes(QUERY, "medium", read_only=True)
    by_key = {r["key"]: r for r in results}
    assert "runner-leases" in by_key, "semantic node must enter via embedding channel"
    assert by_key["runner-leases"]["match_channel"] == "embedding"
    assert "embedding" in channels


def test_load_tree_nodes_below_threshold_not_widened(tmp_tree, monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    raw = {"tree:system/runner-leases": 0.10, "tree:system/shutdown-steps": 0.5}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: raw)
    results, _ = _retrieve.load_tree_nodes(QUERY, "medium", read_only=True)
    keys = {r["key"] for r in results}
    assert "runner-leases" not in keys


# ── _score_weight_limit bonus swap ───────────────────────────────────────────

def test_scorer_uses_embedding_bonus_over_tfidf():
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    a = {"depth": 2, "confidence": 0.5, "summary": "alpha node"}
    b = {"depth": 2, "confidence": 0.5, "summary": "beta node"}
    matched = [("a", a), ("b", b)]
    channels = {"a": "substring", "b": "substring"}
    # Identical base signals; embedding cosine is the only differentiator.
    out = _retrieve._score_weight_limit(matched, channels, 10,
                                        query_text="q", all_nodes=None,
                                        emb_scores={"a": 0.1, "b": 0.9})
    assert [e[0] for e in out] == ["b", "a"]
    # And the bonus magnitude reflects COSINE_BONUS_WEIGHT * cosine.
    eff = {e[0]: e[4] for e in out}  # base score at index 4
    assert eff["b"] - eff["a"] == pytest.approx(
        _retrieve.COSINE_BONUS_WEIGHT * 0.8, abs=1e-6)


def test_scorer_without_emb_scores_unchanged_shape():
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)
    a = {"depth": 2, "confidence": 0.5, "summary": "alpha node"}
    out = _retrieve._score_weight_limit([("a", a)], {"a": "substring"}, 10)
    assert len(out) == 1 and out[0][0] == "a"
