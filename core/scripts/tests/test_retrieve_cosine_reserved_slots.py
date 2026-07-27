"""test_retrieve_cosine_reserved_slots.py — regression tests for the 
cosine slot reservation in `retrieve._score_weight_limit`.

THE DEFECT. The semantic cosine bonus is ADDITIVE (at most COSINE_BONUS_WEIGHT
= 2.0 on a ~4.5-5.4 base, roughly 25%), while `utility_weight` and the MMR
path-similarity penalty act on the WHOLE base. A node could therefore hold the
highest cosine of ANY node for a query and still never be returned. Measured
2026-07-26 against the live 12-query tree-embed harness: `server-lifecycle`
scored cosine 0.5653 — top cosine for its query — and was dropped while 27
SUB-FLOOR nodes were returned (base 4.481 -> utility_weight 0.705 -> pre-MMR
rank 21 of 61 -> MMR dropped it as path-redundant with sibling server/session
nodes ranked above it).

THE FIX. Reserve the top-N floor-clearing nodes by semantic cosine, fill the
remaining (limit - N) slots with the unchanged MMR pass, then re-sort the union
by effective score. Reserved nodes are GUARANTEED a slot but NOT promoted —
they land in their natural effective-score position, so exclusion is fixed
without distorting the returned order.

Measured effect on the 12-query harness (depth=medium): queries having at least
one floor-clearing node whose TOP-cosine node is returned went 8/10 -> 10/10;
total eligible-but-lost 19 -> 18 of 104. The 2 queries still not returning
their top-cosine node have ZERO floor-clearing nodes (top cosine 0.333/0.334 vs
floor 0.35) — correct eligibility behavior, not downstream loss.

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

FLOOR = 0.35


def _cfg(reserved):
    cfg = dict(R._DEFAULT_RETRIEVAL_CFG)
    cfg["cosine_reserved_slots"] = reserved
    cfg["embedding_min_cosine"] = FLOOR
    return cfg


def _node(key, utility_ratio=1.0):
    """Minimal node carrying every field the scorer reads."""
    return {
        "file": f"{key}.md",
        "summary": f"summary for {key}",
        "depth": 2,
        "confidence": 0.8,
        "capability_level": "",
        "retrieval_count": 10,
        "utility_ratio": utility_ratio,
        "times_helpful": 2,
        "times_noise": 1,
    }


def _corpus(n=8, prefix="a/b", weak=None):
    """`weak` is the index of a node given a POOR utility_ratio.

    That reproduces the real shape: the defect needs high cosine AND a
    below-average utility_weight, because the cosine bonus is additive while
    utility_weight multiplies the whole base. Live instance: server-lifecycle
    held the top cosine (0.5653) at utility_weight 0.705 against competitors
    at 0.80-0.92, and was excluded.
    """
    keys = [f"{prefix}/n{i}" for i in range(n)]
    nodes = {k: _node(k, 0.0 if i == weak else 1.0)
             for i, k in enumerate(keys)}
    matched = [(k, nodes[k]) for k in keys]
    channels = {k: "word_prefix" for k in keys}
    return keys, nodes, matched, channels


def _run(monkeypatch, reserved, emb, limit=3, n=8, weak=None):
    keys, nodes, matched, channels = _corpus(n, weak=weak)
    monkeypatch.setattr(R, "_load_retrieval_config", lambda: _cfg(reserved))
    out = R._score_weight_limit(
        matched, channels, limit,
        query_text="q", all_nodes=nodes, emb_scores=emb(keys),
    )
    return keys, out


def test_reserved_slot_rescues_top_cosine_node(monkeypatch):
    """The core defect: a top-cosine node the composite drops is returned."""
    # n7 holds the highest cosine but a starved utility_ratio, so the
    # multiplicative utility_weight sinks it below the limit cutoff — the
    # live server-lifecycle shape.
    emb = lambda ks: {k: (0.9 if k == ks[7] else 0.4) for k in ks}

    _, without = _run(monkeypatch, 0, emb, weak=7)
    keys, with_res = _run(monkeypatch, 3, emb, weak=7)

    assert keys[7] not in {e[0] for e in without}, (
        "fixture no longer reproduces the defect — the top-cosine node "
        "survives even with reservation disabled"
    )
    assert keys[7] in {e[0] for e in with_res}, (
        "reservation must guarantee the top-cosine node a slot"
    )


def test_disabled_is_plain_mmr(monkeypatch):
    """cosine_reserved_slots=0 keeps the pre- path exactly."""
    emb = lambda ks: {k: (0.9 if k == ks[7] else 0.4) for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["n_in"] = len(scored)
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 0, emb, limit=3, n=8)
    # Disabled => MMR receives the FULL pool and the FULL limit.
    assert seen == {"n_in": 8, "limit": 3}


def test_reservation_shrinks_mmr_pool_and_limit(monkeypatch):
    """Reserved nodes are withheld from MMR, which fills only the remainder."""
    emb = lambda ks: {k: (0.9 if k == ks[7] else 0.4) for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["n_in"] = len(scored)
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 3, emb, limit=3, n=8)
    # 3 reserved, but capped at limit-1=2 so MMR keeps authority over >=1 slot.
    assert seen["limit"] == 1
    assert seen["n_in"] == 6


def test_never_reserves_every_slot(monkeypatch):
    """Reservation is capped at limit-1 so MMR always keeps a slot."""
    emb = lambda ks: {k: 0.9 for k in ks}          # every node floor-clearing
    _, out = _run(monkeypatch, 99, emb, limit=4, n=10)
    assert len(out) == 4


def test_subfloor_nodes_are_never_reserved(monkeypatch):
    """A node below the eligibility floor must not win a reserved slot."""
    # n7 has the highest cosine of the pool but is still BELOW the floor.
    emb = lambda ks: {k: (0.30 if k == ks[7] else 0.10) for k in ks}
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    _run(monkeypatch, 3, emb, limit=3, n=8)
    # Nothing eligible => no reservation => MMR gets the full limit.
    assert seen["limit"] == 3


def test_tfidf_path_untouched(monkeypatch):
    """With no embedding scores the TF-IDF path must not reserve anything."""
    seen = {}
    orig = R._mmr_rerank

    def spy(scored, all_nodes, limit, *a, **kw):
        seen["limit"] = limit
        return orig(scored, all_nodes, limit, *a, **kw)

    monkeypatch.setattr(R, "_mmr_rerank", spy)
    keys, nodes, matched, channels = _corpus(8)
    monkeypatch.setattr(R, "_load_retrieval_config", lambda: _cfg(3))
    R._score_weight_limit(matched, channels, 3, query_text="q",
                          all_nodes=nodes, emb_scores={})
    assert seen["limit"] == 3


@pytest.mark.parametrize("reserved", [0, 1, 3, 99])
def test_output_has_no_duplicates_and_respects_limit(reserved, monkeypatch):
    emb = lambda ks: {k: (0.9 if i % 3 == 0 else 0.4)
                      for i, k in enumerate(ks)}
    _, out = _run(monkeypatch, reserved, emb, limit=5, n=12)
    got = [e[0] for e in out]
    assert len(got) == 5
    assert len(set(got)) == len(got), f"duplicate keys in output: {got}"


def test_output_sorted_by_effective_score(monkeypatch):
    """Reserved nodes land in natural rank, not pinned to the top."""
    emb = lambda ks: {k: (0.9 if k == ks[7] else 0.4) for k in ks}
    _, out = _run(monkeypatch, 3, emb, limit=5, n=12)
    effs = [e[2] for e in out]
    assert effs == sorted(effs, reverse=True), (
        f"output must stay ranked by effective score, got {effs}"
    )
