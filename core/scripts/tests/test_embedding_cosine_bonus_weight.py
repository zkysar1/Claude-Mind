"""test_embedding_cosine_bonus_weight.py — the embedding-path cosine weight
(tree.yaml retrieval: embedding_cosine_bonus_weight, 2026-09-03).

COSINE_BONUS_WEIGHT (2.0) was tuned for the TF-IDF fallback. Real embedding
cosines span a narrow band (relevant 0.50-0.65 over a 0.32 floor), so at 2.0
the query-dependent term was worth ~0.6 points while the query-INDEPENDENT
terms (depth, confidence, capability, recency, channel base) swing more than
a point — measured on the 12 hand-labeled harness queries the expected node
carried the higher cosine in every ranked case and still lost (hit@1=0,
MRR=0.123; at W=12: hit@1=5, MRR=0.570). Invariants pinned here:

  1. At the shipped default a stronger cosine outranks a stronger STATIC
     profile (depth 3 + confidence 0.9 + CALIBRATE vs depth 1 + 0.5).
  2. An absent or malformed key degrades to COSINE_BONUS_WEIGHT — the
     pre-change ordering — never to an unverified value.
  3. The TF-IDF fallback path (no embedding scores) ignores the key.

Same bootstrap as test_embedding_channel_status.py.
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
_TMPDIR = tempfile.mkdtemp(prefix="embedding-cosw-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location(
    "retrieve_cosw_mod", CORE_SCRIPTS / "retrieve.py")
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

from tree_match import COSINE_BONUS_WEIGHT  # noqa: E402

# a: the better SEMANTIC match with a weak static profile.
# b: a marginal semantic match with the strongest static profile the scorer
#    hands out (depth 3 +1.5, confidence 0.9*0.9, CALIBRATE +0.1): 1.66 points
#    of static edge over a. Cosine edge for a: 0.25, so a wins iff W > 6.64.
NODE_A = {"key": "a", "depth": 1, "confidence": 0.5, "capability_level": "",
          "retrieval_count": 0}
NODE_B = {"key": "b", "depth": 3, "confidence": 0.9, "capability_level": "CALIBRATE",
          "retrieval_count": 0}
EMB = {"a": 0.60, "b": 0.35}
CHANNELS = {"a": "embedding", "b": "embedding"}


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


def _order(cfg, emb=EMB):
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    out = _retrieve._score_weight_limit(
        [("a", dict(NODE_A)), ("b", dict(NODE_B))], CHANNELS, 10,
        query_text="", all_nodes=None, emb_scores=emb)
    return [e[0] for e in out]


def _cfg(**over):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg.update(over)
    return cfg


def test_default_config_carries_the_measured_weight():
    assert _retrieve._DEFAULT_RETRIEVAL_CFG["embedding_cosine_bonus_weight"] == 12.0


def test_shipped_default_lets_cosine_outrank_static_profile():
    assert _order(_cfg()) == ["a", "b"]


def test_absent_key_degrades_to_tfidf_weight():
    cfg = _cfg()
    cfg.pop("embedding_cosine_bonus_weight", None)
    # at COSINE_BONUS_WEIGHT (2.0) the static profile wins — the pre-change order
    assert COSINE_BONUS_WEIGHT == 2.0
    assert _order(cfg) == ["b", "a"]


def test_malformed_key_degrades_to_tfidf_weight():
    assert _order(_cfg(embedding_cosine_bonus_weight="twelve")) == ["b", "a"]


def test_key_value_is_honoured_both_ways():
    assert _order(_cfg(embedding_cosine_bonus_weight=2.0)) == ["b", "a"]
    assert _order(_cfg(embedding_cosine_bonus_weight=12.0)) == ["a", "b"]


def test_tfidf_path_ignores_the_key():
    # No embedding scores and no corpus => no cosine term at all; only the
    # static profile ranks, whatever the key says.
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(embedding_cosine_bonus_weight=100.0)
    out = _retrieve._score_weight_limit(
        [("a", dict(NODE_A)), ("b", dict(NODE_B))], CHANNELS, 10,
        query_text="", all_nodes=None, emb_scores=None)
    assert [e[0] for e in out] == ["b", "a"]
