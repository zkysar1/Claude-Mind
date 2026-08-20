"""refuse_check() — the pure enforcement-tier verdict ().

Endpoint-level behavior (409, increment, allow_near_dup pop) is pinned in
mind_api/tests/test_runtime_store_dupe_refuse.py; this file pins the pure
function and the threshold configuration so a regression is caught by the
default-run suite (mind_api/tests is a deferred testpath).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import store_dupe_warn  # noqa: E402


def _corpus(*texts):
    return [(f"guard-{i}", t) for i, t in enumerate(texts, start=1)]


RULE = ("before repointing the frobnicator manifold always quiesce the flux "
        "capacitor spool and verify the infundibulum drained")


def test_verbatim_twin_yields_verdict_with_evidence():
    v = store_dupe_warn.refuse_check(
        {"rule": RULE}, "guardrails", _corpus(RULE, "an unrelated rule"))
    assert v is not None
    assert v["nearest_id"] == "guard-1"
    assert v["similarity"] == 1.0
    assert v["refuse_threshold"] == 0.75
    assert "frobnicator" in v["nearest_text"]


def test_ambient_similarity_yields_none():
    """p99 of 6,524 measured fleet adds is 0.294; a typical-overlap candidate
    must never be refused."""
    v = store_dupe_warn.refuse_check(
        {"rule": "always check the spool gauge after maintenance"},
        "guardrails",
        _corpus("never trust one signal when concluding a store is empty",
                "verify the flux capacitor before any deploy window"))
    assert v is None


def test_candidate_own_id_is_excluded():
    v = store_dupe_warn.refuse_check(
        {"id": "guard-1", "rule": RULE}, "guardrails", _corpus(RULE))
    assert v is None, "a record must not be refused as a duplicate of itself"


def test_unknown_store_and_empty_candidate_yield_none():
    assert store_dupe_warn.refuse_check({"rule": RULE}, "nope",
                                        _corpus(RULE)) is None
    assert store_dupe_warn.refuse_check({}, "guardrails",
                                        _corpus(RULE)) is None
    assert store_dupe_warn.refuse_check({"rule": RULE}, "guardrails",
                                        []) is None


def test_refuse_threshold_configured_above_warn_for_every_store():
    """The enforcement tier must sit ABOVE the advisory tier — a refuse at or
    below warn would convert the deliberately-chatty advisory band into
    blocks, the exact false-positive harm g-115-3223 scoped the advisory to
    avoid. And it must sit above 0.55: the max ambient nearest-neighbour
    similarity ever measured on a legitimate add is 0.500 (n=6,524,
    2026-08-20), so any refuse threshold at or below the ambient ceiling
    would block clean adds."""
    for store, cfg in store_dupe_warn.STORES.items():
        thr = cfg.get("refuse_threshold")
        assert thr is not None, f"{store}: refuse_threshold missing"
        assert thr > cfg["threshold"], f"{store}: refuse <= warn"
        assert thr > 0.55, f"{store}: refuse inside the measured ambient band"
        assert thr <= 1.0, f"{store}: unreachable refuse threshold"


def test_reasoning_bank_keys_on_title_for_refusal():
    title = "Quiesce the frobnicator flux spool before manifold repoint"
    v = store_dupe_warn.refuse_check(
        {"title": title, "content": "totally different content"},
        "reasoning-bank",
        [("rb-1", title)])
    assert v is not None and v["nearest_id"] == "rb-1"
