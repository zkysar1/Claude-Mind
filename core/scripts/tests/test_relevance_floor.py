"""test_relevance_floor.py — unit tests for the supplementary-store relevance
floor (g-115-7318).

Covers retrieve._relevance_floor, the two helpers extracted for it
(_entry_token_corpus / _query_overlap), and all THREE call sites
(load_reasoning_bank domain lane, load_guardrails, load_pattern_signatures).

THE DEFECT BEING PINNED. `_entry_matches` admits supplementary entries on a
BOOLEAN and the cap then orders purely by utilization, so relevance is thrown
away at the door. 3,087 of 4,702 live guardrails carry utilization_score > 0,
so a freshly-encoded record at 0.0 cannot win a cap slot at any depth — and it
cannot earn utilization without being returned. Measured 2026-08-24: guard-4838
ranked 377 of 431 on the query its own consumers are told to run; guard-4902
ranked 27 of 932 on verbatim text from its own rule field.

The load-bearing invariants:

  1. EXTRACTION EQUIVALENCE — `_entry_matches_text` still means exactly
     ">= 2 distinct length->=5 query tokens", across every field shape.
  2. NO-OP WHEN NOTHING IS CUT — len(ranked) <= cap returns the SAME object.
  3. DISABLE — relevance_reserved_slots 0 is byte-identical to pre-g-115-7318.
  4. PROMOTION — a zero-utility high-overlap entry past the cap reaches the head.
  5. BOUNDED DISPLACEMENT — the top (cap - slots) by utility always survive, in
     order. This is the "no known-good knowledge hidden" property; without it
     the floor would be a full relevance re-sort, which measurement rejected
     (20 of 20 top slots churned).
  6. PERMUTATION — never drops or duplicates a record.
  7. THRESHOLD CLAMP — a configured min_overlap at or below the admission
     threshold (2) is clamped strictly above it, so bare-minimum accidental
     matches (370 of 431 candidates on the live corpus) can never claim a slot.
  8. CALL-SITE WIRING (guard-3625) — the mechanism and its callers ship
     together, so each lane is proven end-to-end, not just the helper.
  9. as_of READS KEEP THE FLOOR — the deliberate divergence from
     `_embedding_blend`, which skips historical reads because it ranks them
     against a CURRENT semantic index. This floor reads only the query and the
     entry's own stored text.

Same bootstrap as test_embedding_blend.py.
"""
from __future__ import annotations

import importlib.util
import json
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
_TMPDIR = tempfile.mkdtemp(prefix="relevance-floor-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location(
    "retrieve_relfloor_mod", CORE_SCRIPTS / "retrieve.py")
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT


def _cfg(**over):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg()
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


# Six distinct length->=5 tokens. Short words ("the", "a") are invisible to the
# matcher by design, so every token here is load-bearing.
QUERY = ["freshly encoded guardrail retrieval invisible utility"]


def _rec(rid, rule, util=0.0, category="framework-retrieval", **extra):
    r = {
        "id": rid, "status": "active", "category": category, "rule": rule,
        "created": "2026-08-01T00:00:00",
        "utilization": {"utilization_score": util, "retrieval_count": 0},
    }
    r.update(extra)
    return r


# ── 1. Extraction equivalence ────────────────────────────────────────────────

@pytest.mark.parametrize("entry,expected", [
    ({}, False),                                                  # no fields
    ({"rule": "guardrail retrieval"}, True),                      # 2 tokens
    ({"rule": "guardrail only"}, False),                          # 1 token
    ({"rule": None, "title": 7}, False),                          # non-str fields
    ({"tags": ["guardrail", "retrieval"]}, True),                 # tags list
    ({"tags": "guardrail retrieval"}, False),                     # tags not a list
    ({"when_to_use": {"conditions": ["guardrail", "utility"]}}, True),
    ({"when_to_use": {"conditions": "guardrail utility"}}, True),  # conditions str
    ({"summary": "encoded invisible"}, True),
    ({"content": "the a of it"}, False),                          # all tokens < 5
])
def test_entry_matches_text_semantics_unchanged(entry, expected):
    assert _retrieve._entry_matches_text(entry, QUERY) is expected


def test_entry_matches_text_empty_categories_is_false():
    assert _retrieve._entry_matches_text({"rule": "guardrail retrieval"}, []) is False


def test_query_overlap_is_max_over_queries_not_sum():
    e = {"rule": "guardrail retrieval invisible"}
    # Two alternative queries; _entry_matches_text admits on ANY, so the entry's
    # relevance is its BEST query, never the total across them.
    assert _retrieve._query_overlap(e, ["guardrail retrieval", "invisible"]) == 2


def test_query_overlap_counts_distinct_tokens_only():
    e = {"rule": "guardrail guardrail guardrail retrieval"}
    assert _retrieve._query_overlap(e, ["guardrail guardrail retrieval"]) == 2


def test_entry_token_corpus_empty_for_fieldless_entry():
    assert _retrieve._entry_token_corpus({"id": "x"}) == set()


# ── 2/3. No-op paths ─────────────────────────────────────────────────────────

def test_no_op_when_nothing_is_cut():
    ranked = [_rec("g-1", "guardrail retrieval invisible utility encoded freshly")]
    assert _retrieve._relevance_floor(ranked, QUERY, cap=20) is ranked


def test_zero_slots_disables_byte_identically():
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(relevance_reserved_slots=0)
    ranked = ([_rec(f"g-{i}", "unrelated filler words here", util=0.5) for i in range(3)]
              + [_rec("g-hit", "freshly encoded guardrail retrieval invisible utility")])
    assert _retrieve._relevance_floor(ranked, QUERY, cap=3) is ranked


def test_negative_or_garbage_config_degrades_to_default_without_raising():
    for bad in ("banana", None, -5, [1, 2]):
        _retrieve._RETRIEVAL_CFG_CACHE = _cfg(relevance_reserved_slots=bad)
        ranked = ([_rec(f"g-{i}", "unrelated filler words here", util=0.5)
                   for i in range(5)]
                  + [_rec("g-hit", "freshly encoded guardrail retrieval invisible utility")])
        out = _retrieve._relevance_floor(ranked, QUERY, cap=3)
        assert len(out) == len(ranked)          # never raises, never drops
        if bad in ("banana", None, [1, 2]):     # -> default 3
            assert out[0]["id"] == "g-hit"
        else:                                   # -5 clamps to 0 -> disabled
            assert out is ranked


# ── 4/5/6. The core behaviour ────────────────────────────────────────────────

def _corpus():
    """3 high-utility near-misses (2-token overlap), then the real answer."""
    return [
        _rec("g-top", "utility guardrail filler alpha", util=0.80),
        _rec("g-two", "utility guardrail filler beta", util=0.70),
        _rec("g-three", "utility guardrail filler gamma", util=0.60),
        _rec("g-four", "utility guardrail filler delta", util=0.50),
        _rec("g-hit", "freshly encoded guardrail retrieval invisible utility", util=0.0),
    ]


def test_high_overlap_zero_utility_entry_is_promoted_into_the_head():
    out = _retrieve._relevance_floor(_corpus(), QUERY, cap=4)
    assert out[0]["id"] == "g-hit", [r["id"] for r in out[:4]]


def test_top_utility_entries_survive_in_order():
    # slots=3, cap=4 -> exactly ONE utility slot is guaranteed... but only one
    # candidate clears the overlap threshold, so only one slot is consumed and
    # the top THREE by utility keep their positions behind it.
    out = _retrieve._relevance_floor(_corpus(), QUERY, cap=4)
    assert [r["id"] for r in out[:4]] == ["g-hit", "g-top", "g-two", "g-three"]


def test_output_is_a_permutation_of_the_input():
    ranked = _corpus()
    out = _retrieve._relevance_floor(ranked, QUERY, cap=4)
    assert sorted(r["id"] for r in out) == sorted(r["id"] for r in ranked)
    assert len(out) == len(ranked)


def test_promotion_is_capped_by_slots():
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(relevance_reserved_slots=1)
    ranked = [_rec(f"g-{i}", "utility guardrail filler", util=0.9 - i / 100)
              for i in range(6)]
    ranked += [
        _rec("g-hit1", "freshly encoded guardrail retrieval invisible utility"),
        _rec("g-hit2", "freshly encoded guardrail retrieval invisible"),
    ]
    out = _retrieve._relevance_floor(ranked, QUERY, cap=6)
    head = [r["id"] for r in out[:6]]
    assert head[0] == "g-hit1"          # strongest overlap (6) wins the one slot
    assert "g-hit2" not in head         # the second is NOT promoted


def test_ties_on_overlap_break_by_existing_utility_rank():
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(relevance_reserved_slots=1)
    ranked = [_rec(f"g-{i}", "utility guardrail filler", util=0.9) for i in range(4)]
    # Identical overlap; the earlier one arrives with the higher utility rank.
    ranked += [_rec("g-eq-hi", "freshly encoded guardrail retrieval", util=0.2),
               _rec("g-eq-lo", "freshly encoded guardrail retrieval", util=0.1)]
    out = _retrieve._relevance_floor(ranked, QUERY, cap=4)
    assert out[0]["id"] == "g-eq-hi"


def test_equal_overlap_candidate_is_not_promoted():
    """Eligibility is not improvement — a tie goes to the incumbent.

    Regression pin for the defect test_embedding_blend's cap-after-widen test
    caught: 21 guardrails at IDENTICAL overlap, one of them below the cap.
    Promoting it swaps an equally-relevant record for a strictly less-proven
    one and evicts a better entry for nothing.
    """
    ranked = [_rec(f"g-{i}", "guardrail retrieval invisible filler",
                   util=0.9 - i / 100) for i in range(6)]
    assert _retrieve._query_overlap(ranked[0], QUERY) == 3          # control
    out = _retrieve._relevance_floor(ranked, QUERY, cap=5)
    assert out is ranked


def test_strictly_greater_overlap_is_promoted_over_the_tie_case():
    """The positive control for the test above: raise the candidate by ONE
    token and the identical arrangement now promotes."""
    ranked = [_rec(f"g-{i}", "guardrail retrieval invisible filler",
                   util=0.9 - i / 100) for i in range(5)]
    ranked.append(_rec("g-better", "guardrail retrieval invisible encoded", util=0.0))
    assert _retrieve._query_overlap(ranked[-1], QUERY) == 4         # control
    out = _retrieve._relevance_floor(ranked, QUERY, cap=5)
    assert out[0]["id"] == "g-better"


def test_promotion_stops_at_the_first_non_gain():
    """Promotions are taken greedily and STOP at the first tie, so one strong
    candidate cannot drag weaker ties in behind it."""
    ranked = [_rec(f"g-{i}", "guardrail retrieval invisible filler",
                   util=0.9 - i / 100) for i in range(6)]      # overlap 3
    ranked.append(_rec("g-strong", "freshly encoded guardrail retrieval invisible utility"))
    ranked.append(_rec("g-tie", "guardrail retrieval invisible other"))   # overlap 3
    out = _retrieve._relevance_floor(ranked, QUERY, cap=6)
    ids = [r["id"] for r in out[:6]]
    assert ids[0] == "g-strong"
    assert "g-tie" not in ids


# ── 7. Threshold clamp ───────────────────────────────────────────────────────

@pytest.mark.parametrize("configured", [0, 1, 2, -3, "banana"])
def test_min_overlap_is_clamped_strictly_above_the_admission_threshold(configured):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(relevance_floor_min_overlap=configured)
    # This entry clears ADMISSION (2 tokens) but must never claim a floor slot:
    # on the live corpus 370 of 431 candidates look exactly like this.
    ranked = [_rec(f"g-{i}", "utility guardrail alpha beta", util=0.9 - i / 100)
              for i in range(5)]
    ranked += [_rec("g-bare", "guardrail retrieval and nothing else", util=0.0)]
    out = _retrieve._relevance_floor(ranked, QUERY, cap=5)
    assert out is ranked, "a bare 2-token match must not be promoted"


def test_min_overlap_three_admits_a_three_token_match():
    ranked = [_rec(f"g-{i}", "utility guardrail alpha", util=0.9 - i / 100)
              for i in range(5)]
    ranked += [_rec("g-three-tok", "guardrail retrieval invisible", util=0.0)]
    out = _retrieve._relevance_floor(ranked, QUERY, cap=5)
    assert out[0]["id"] == "g-three-tok"


def test_strict_category_query_with_no_token_overlap_promotes_nothing():
    # An exact-category request never carries free-text tokens, so the floor is
    # inert there — the lane it fixes is free-text retrieval.
    ranked = [_rec(f"g-{i}", "alpha beta gamma delta", util=0.9 - i / 100,
                   category="framework-retrieval") for i in range(6)]
    out = _retrieve._relevance_floor(ranked, ["framework-retrieval"], cap=5)
    assert out is ranked


# ── 8/9. Call-site wiring, end to end ────────────────────────────────────────

def _write(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture()
def stores(tmp_path):
    saved = (_retrieve.RB_PATH, _retrieve.GUARD_PATH, _retrieve.SIGS_PATH)
    _retrieve.RB_PATH = tmp_path / "reasoning-bank.jsonl"
    _retrieve.GUARD_PATH = tmp_path / "guardrails.jsonl"
    _retrieve.SIGS_PATH = tmp_path / "pattern-signatures.jsonl"
    yield tmp_path
    (_retrieve.RB_PATH, _retrieve.GUARD_PATH, _retrieve.SIGS_PATH) = saved


def _cold_start_corpus(prefix, n_noise=40):
    """n_noise established 2-token matches + one cold high-overlap entry."""
    recs = [_rec(f"{prefix}-{i}", "utility guardrail filler text",
                 util=0.9 - i / 1000) for i in range(n_noise)]
    recs.append(_rec(f"{prefix}-cold",
                     "freshly encoded guardrail retrieval invisible utility",
                     util=0.0))
    return recs


def test_load_guardrails_returns_the_cold_entry(stores):
    _write(_retrieve.GUARD_PATH, _cold_start_corpus("guard"))
    out = _retrieve.load_guardrails(QUERY, depth="shallow", read_only=True)
    assert len(out) == 20
    assert out[0]["id"] == "guard-cold"


def test_load_guardrails_bump_set_equals_return_set(stores, monkeypatch):
    """The invariant is about the PREDICATE, so intercept the predicate.

    An earlier version of this test asserted on retrieval_count inside the
    JSONL and read set() — because for a sidecar-covered kind the bump is
    SPOOL-ROUTED (g-358-22) and never rewrites the store. That is guard-4631 /
    guard-2298 in one line: an empty result from a shape nobody verified. The
    destination is an implementation detail that has already moved once;
    `should_bump_fn` is the contract, so capture that instead.
    """
    records = _cold_start_corpus("guard")
    _write(_retrieve.GUARD_PATH, records)
    seen = {}

    def _capture(path, should_bump_fn, **kw):
        for r in records:
            seen[r["id"]] = bool(should_bump_fn(r))
        return records

    monkeypatch.setattr(_retrieve, "_locked_bump_jsonl", _capture)
    out = _retrieve.load_guardrails(QUERY, depth="shallow", read_only=False)
    returned = {r["id"] for r in out}
    bumped = {rid for rid, hit in seen.items() if hit}
    assert seen, "the bump path must actually run on a read_only=False call"
    assert bumped == returned, "the floor must not break bump-set == return-set"
    assert "guard-cold" in bumped, "a promoted entry must be bumped, or it can never earn utilization"


def test_load_guardrails_as_of_read_still_gets_the_floor(stores):
    # The deliberate divergence from _embedding_blend, which skips as_of.
    _write(_retrieve.GUARD_PATH, _cold_start_corpus("guard"))
    out = _retrieve.load_guardrails(QUERY, depth="shallow",
                                    as_of="2026-08-25T00:00:00")
    assert out and out[0]["id"] == "guard-cold"


def test_load_reasoning_bank_domain_lane_returns_the_cold_entry(stores):
    recs = _cold_start_corpus("rb")
    for r in recs:                       # domain lane, not universal
        r["applies_to"] = "specific"
        r["content"] = r["rule"]
    _write(_retrieve.RB_PATH, recs)
    domain, _universal = _retrieve.load_reasoning_bank(
        QUERY, depth="shallow", read_only=True)
    assert len(domain) == 20
    assert domain[0]["id"] == "rb-cold"


def test_load_pattern_signatures_below_cap_is_unchanged(stores):
    recs = [_rec("sig-1", "utility guardrail filler", util=0.9),
            _rec("sig-2", "freshly encoded guardrail retrieval invisible")]
    _write(_retrieve.SIGS_PATH, recs)
    out = _retrieve.load_pattern_signatures(QUERY, depth="shallow", read_only=True)
    assert [r["id"] for r in out] == ["sig-1", "sig-2"]
