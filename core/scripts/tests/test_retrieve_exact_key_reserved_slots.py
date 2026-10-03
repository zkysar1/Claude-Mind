"""test_retrieve_exact_key_reserved_slots.py — regression tests for the
g-115-11525 exact-key slot reservation in `retrieve._score_weight_limit`.

THE DEFECT (g-115-11525). `exact_key` is the strongest LEXICAL channel
(CHANNEL_SCORES 4.0): the query IS the node's key. But it had no guaranteed
slot. Its base lead over a semantic neighbour is a fraction of a point (the
channel gap 4.0 vs the neighbour's 1.5-2.5 plus the depth, confidence and
recency scoring terms), so
either of these flips the pre-MMR sort:

  (a) a `utility_weight` penalty on the key itself — times_noise > 0 drives
      utility_ratio down and the weight multiplies the WHOLE base (the
      multiplicative factor the additive cosine bonus cannot outrun), or
  (b) a high REAL cosine on the neighbour — at the shipped
      embedding_cosine_bonus_weight (12.0) even a 0.30 cosine gap is 3.6
      points, more than any channel gap.

`_mmr_rerank` then drops the demoted key as redundant, and the node queried by
its own exact key is absent from the page. The window is widest between the
node's write and the next embedding-index build, when the unindexed-floor
imputation (g-115-3684) is the node's only edge — exactly when the operator
is querying for what they just wrote (g-374-34 outcome 3). Measured 2026-09-29
on a frozen snapshot: `claude-code-vs-zakpod-goal-throughput` was MATCHED
(channel exact_key, 1 of 31 candidates) but ranked 13 of 109 pre-MMR and was
absent from the 15-slot shallow page.

THE FIX. Reserve the top-N `exact_key`-channel nodes out of the pool, fill the
remaining slots with the unchanged MMR pass, re-sort the union by effective
score — the same shape as `cosine_reserved_slots` (g-306-93): reserved nodes
are GUARANTEED a slot but NOT promoted. Channel-based, so BOTH the TF-IDF
path and the real-embedding path are protected. 0 disables (byte-identical to
pre-g-115-11525).

COLLECTION-SAFETY: pure in-process unit tests over synthetic nodes. No env
pins, no tmp world, no subprocess, no live-tree reads — so pytest's shared
process cannot be poisoned and the live retrieval index is never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retrieve as R  # noqa: E402

FLOOR = 0.32  # embedding_tree_min_cosine, the unindexed-floor imputation


def _cfg(exact_reserved, cosine_reserved=0, cosine_weight=None):
    cfg = dict(R._DEFAULT_RETRIEVAL_CFG)
    cfg["exact_key_reserved_slots"] = exact_reserved
    cfg["cosine_reserved_slots"] = cosine_reserved
    cfg["embedding_tree_min_cosine"] = FLOOR
    cfg["embedding_min_cosine"] = FLOOR
    # The shipped embedding weight (12.0) is what makes case (b) real; keep
    # it so the fixtures exercise the production-scale cosine term.
    cfg["embedding_cosine_bonus_weight"] = (
        R._DEFAULT_RETRIEVAL_CFG["embedding_cosine_bonus_weight"]
        if cosine_weight is None else cosine_weight)
    # _utility_weight is CENTERED: w = clamp(1.0 + (utility_ratio - center)).
    # The DEFAULT center is 0.0, where utility_ratio=0.0 gives w=1.0 —
    # NEUTRAL, no penalty, and the times_noise story of this goal would not
    # be the defect. tree.yaml ships the measured corpus-mean center
    # (0.2061, derived there — ); with it, ur=0.0 (noise-dominated,
    # no helpful credit) gives w = 1.0 - 0.2061 = 0.7939 while a healthy node
    # at ur=1.0 caps at w=1.5 — enough to flip the 2.5-point channel lead
    # (exact_key base 6.22 vs word_prefix 3.72 on depth-3 confidence-0.8
    # fixtures, probed from _compute_match_score + provenance 0.9,
    # ):
    # 6.22*0.7939 = 4.94 < 3.72*1.5 = 5.58.
    cfg["utility_weight_center"] = 0.2061
    return cfg


def _node(key, utility_ratio=1.0, times_noise=1, depth=3):
    """Minimal node carrying every field the scorer reads.

    retrieval_count >= 5 and a nonzero times_noise send the node down the
    utility-ratio path of _utility_weight (no early-return 1.0), so
    utility_ratio controls its multiplicative weight.
    """
    return {
        "file": f"{key}.md",
        "summary": f"summary for {key}",
        "depth": depth,
        "confidence": 0.8,
        "capability_level": "",
        "retrieval_count": 10,
        "utility_ratio": utility_ratio,
        "times_helpful": 2,
        "times_noise": times_noise,
    }


def _corpus(n=12, key_idx=0, key_channel="exact_key", weak_key=False):
    """`key_idx` is the reserved-channel node; the rest are word_prefix.

    The reserved node is the one a query names by its own key; its channel
    defaults to `exact_key` but can be overridden to prove the reservation is
    channel-scoped (a non-exact channel never reserves). `weak_key` puts the
    times_noise-driven utility penalty (utility_ratio 0.0) on THAT node — the
    goal's times_noise > 0 case — BEFORE `matched` is built, so every consumer
    sees the same node object (a matched-list built from a pre-swap dict
    would silently score the healthy twin).
    """
    keys = [f"a/b/n{i}" for i in range(n)]
    nodes = {}
    for i, k in enumerate(keys):
        if i == key_idx and weak_key:
            nodes[k] = _node(k, utility_ratio=0.0, times_noise=3)
        else:
            nodes[k] = _node(k)
    matched = [(k, nodes[k]) for k in keys]
    channels = {k: "word_prefix" for k in keys}
    channels[keys[key_idx]] = key_channel
    return keys, nodes, matched, channels


def _run(monkeypatch, exact_reserved, emb, limit=5, n=12, key_idx=0,
         key_channel="exact_key", cosine_reserved=0, weak_key=True):
    """Score the fixture. `weak_key` keeps the times_noise-driven utility
    penalty (utility_ratio 0.0) on the reserved node."""
    keys, nodes, matched, channels = _corpus(n, key_idx, key_channel,
                                             weak_key)
    monkeypatch.setattr(R, "_load_retrieval_config",
                        lambda: _cfg(exact_reserved, cosine_reserved))
    out = R._score_weight_limit(
        matched, channels, limit,
        query_text="", all_nodes=nodes, emb_scores=emb(keys),
    )
    return keys, out


def test_unindexed_noisy_exact_key_node_is_rescued(monkeypatch):
    """THE CORE DEFECT (outcome 1, both required shapes at once): a node
    queried by its exact key, ABSENT from the embedding index (unindexed-floor
    imputation is its only cosine) AND carrying times_noise > 0, is dropped
    without the reservation and returned with it.

    Complementary-mutation pair (guard-2435): the same fixture is asserted
    ABSENT at exact_key_reserved_slots=0 and PRESENT at 1 — reservation off
    reddens this case, reservation on reddens it the other way. Neither half
    is decoration.
    """
    # Rivals carry real cosines (0.6) and a healthy utility weight; the
    # target gets the unindexed floor (FLOOR) and the noise-driven weight.
    # At the shipped cosine weight the target sits deep below the limit cut.
    emb = lambda ks: {k: 0.6 for k in ks if k != ks[0]}

    keys, without = _run(monkeypatch, 0, emb, weak_key=True)
    assert keys[0] not in {e[0] for e in without}, (
        "fixture no longer reproduces the defect — the unindexed noisy "
        "exact-key node survives even with reservation disabled")

    _, with_res = _run(monkeypatch, 1, emb, weak_key=True)
    assert keys[0] in {e[0] for e in with_res}, (
        "reservation must guarantee the unindexed noisy exact-key node a slot")


def test_indexed_noisy_exact_key_node_is_rescued(monkeypatch):
    """Outcome 1's second shape: the node IS indexed (a real cosine, barely
    above the floor) but times_noise > 0 still sinks it past the limit cut.
    The reservation protects the channel, not the index state."""
    emb = lambda ks: {ks[0]: 0.35, **{k: 0.62 for k in ks[1:]}}

    keys, without = _run(monkeypatch, 0, emb, weak_key=True)
    assert keys[0] not in {e[0] for e in without}, (
        "fixture no longer reproduces the defect — the indexed noisy "
        "exact-key node survives without reservation")

    _, with_res = _run(monkeypatch, 1, emb, weak_key=True)
    assert keys[0] in {e[0] for e in with_res}


def test_tfidf_path_is_protected(monkeypatch):
    """The reservation is CHANNEL-based, so the TF-IDF path (no embedding
    scores at all) is protected by the same config key. This is the box shape
    with no per-box index: query-by-key still cannot be lost to a
    utility_weight penalty plus MMR."""
    emb = lambda ks: {}  # use_emb is False -> TF-IDF branch (no bonus for "")

    keys, without = _run(monkeypatch, 0, emb, weak_key=True)
    assert keys[0] not in {e[0] for e in without}, (
        "fixture no longer reproduces the defect on the TF-IDF path")

    _, with_res = _run(monkeypatch, 1, emb, weak_key=True)
    assert keys[0] in {e[0] for e in with_res}


def test_disabled_is_plain_mmr(monkeypatch):
    """exact_key_reserved_slots=0 keeps the pre- path exactly:
    MMR receives the FULL pool and the FULL limit."""
    emb = lambda ks: {k: 0.6 for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["n_in"] = len(scored)
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 0, emb)
    assert seen == {"n_in": 12, "limit": 5}


def test_reservation_shrinks_mmr_pool_and_limit(monkeypatch):
    """The reserved node is withheld from MMR, which fills only the remainder."""
    emb = lambda ks: {k: 0.6 for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["n_in"] = len(scored)
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 1, emb)
    assert seen == {"n_in": 11, "limit": 4}


def test_only_the_exact_key_channel_reserves(monkeypatch):
    """A top node on ANY other channel (substring, word_prefix, embedding)
    never wins a reserved slot: the reservation is keyed to the exact_key
    channel, not to rank. Positive control that the channel test is real."""
    emb = lambda ks: {k: 0.6 for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["n_in"] = len(scored)
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    # The top node is `substring` (query inside the key, not equal to it):
    # strong lexical match, no guarantee.
    _run(monkeypatch, 2, emb, key_channel="substring")
    assert seen == {"n_in": 12, "limit": 5}, (
        "a non-exact_key channel must not reserve a slot")


def test_no_exact_key_in_pool_no_reservation(monkeypatch):
    """No exact_key node in the pool => nothing to reserve, MMR gets all."""
    emb = lambda ks: {k: 0.6 for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 2, emb, key_channel="word_prefix")
    assert seen["limit"] == 5


def test_never_reserves_every_slot(monkeypatch):
    """Capped at limit-1 so MMR always keeps authority over >=1 slot, even
    when the config asks for more slots than the page has."""
    # Two exact_key nodes in the pool (n=6, key at 0 and 1); ask for 99.
    keys = [f"a/b/n{i}" for i in range(6)]
    nodes = {k: _node(k, utility_ratio=0.0, times_noise=3) for k in keys}
    matched = [(k, nodes[k]) for k in keys]
    channels = {k: "exact_key" for k in keys}
    emb = {k: 0.6 for k in keys}
    monkeypatch.setattr(R, "_load_retrieval_config", lambda: _cfg(99))
    out = R._score_weight_limit(matched, channels, 4, query_text="",
                                all_nodes=nodes, emb_scores=emb)
    assert len(out) == 4


def test_reserved_node_lands_in_natural_position(monkeypatch):
    """Reserved nodes are NOT promoted: the returned page is sorted by
    effective score, and the rescued node sits where its score puts it."""
    emb = lambda ks: {k: 0.6 for k in ks}
    _, out = _run(monkeypatch, 1, emb)
    effs = [e[2] for e in out]
    assert effs == sorted(effs, reverse=True), (
        f"page not sorted by effective score: {effs}")
    assert out[-1][0] == "a/b/n0", (
        "the weak reserved node should sit LAST (natural position), "
        f"not pinned to the top: {[e[0] for e in out]}")


def test_exact_key_precedes_cosine_and_combined_caps(monkeypatch):
    """When both reservations fire: exact_key takes its slots FIRST (the
    stronger lexical claim), cosine fills only the unclaimed room, and the
    combined total never takes every slot."""
    n = 12
    # weak_key BEFORE matched is built (see _corpus): a post-hoc dict swap
    # would leave the matched-list holding the healthy twin.
    keys, nodes, matched, channels = _corpus(n, weak_key=True)
    emb = {k: 0.6 for k in keys}  # every node clears the cosine floor too
    monkeypatch.setattr(R, "_load_retrieval_config",
                        lambda: _cfg(2, cosine_reserved=3))
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["n_in"] = len(scored)
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    out = R._score_weight_limit(matched, channels, 5, query_text="",
                                all_nodes=nodes, emb_scores=emb)
    # Trace (limit=5, one exact_key node in the pool, all 12 clear the
    # cosine floor at 0.6):
    #   exact_key: min(2, limit-1)=2 slots wanted, 1 node available -> 1
    #              reserved. The exact node is claimed FIRST.
    #   cosine:    room = limit-1-len(reserved) = 5-1-1 = 3; reserves
    #              min(3, 3) = 3 of the 11 non-exact nodes (the exact node
    #              is excluded from eligibility, so cosine never reclaims
    #              it).
    #   total reserved = 4 < 5 -> MMR keeps authority over 5-4 = 1 slot,
    #              filling it from the 12-4 = 8 unreserved nodes.
    assert seen["limit"] == 1
    assert seen["n_in"] == 8
    assert len(out) == 5
    assert keys[0] in {e[0] for e in out}


@pytest.mark.parametrize("exact_reserved", [0, 1, 2, 99])
def test_no_duplicates_and_limit_respected(exact_reserved, monkeypatch):
    emb = lambda ks: {k: (0.6 if i % 3 == 0 else 0.35)
                      for i, k in enumerate(ks)}
    _, out = _run(monkeypatch, exact_reserved, emb, limit=5, n=12)
    got = [e[0] for e in out]
    assert len(got) == 5
    assert len(set(got)) == len(got), f"duplicate keys in output: {got}"


def test_limit_one_disables_reservation(monkeypatch):
    """At limit=1 there is no room to reserve (limit>1 guard); the single
    slot goes to the best-scoring node by the plain path — reservation must
    not crash or steal it."""
    emb = lambda ks: {k: 0.6 for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 2, emb, limit=1)
    assert seen["limit"] == 1
