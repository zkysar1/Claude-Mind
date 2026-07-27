"""Tests for merge_pattern_signatures ().

pattern-signatures.jsonl was the last g-115-2319 store neither registered nor
adjudicated in coordination_merge._HANDLERS. Its writers mutate records in
place (pattern_signatures_write.record_outcome bumps outcome_stats and
recomputes accuracy; set-status retires), so an unregistered both-diverged 412
froze the file fleet-wide (rb-3150 class) — and line-union would resurrect
retired signatures. The handler is the id-keyed field-merge shape
(merge_reasoning_bank / merge_guardrails) with three store-specific rules:

  - outcome_stats: confirmed/total counter-MAX, accuracy RECOMPUTED from the
    merged counters (writer parity: round(c/t, 4)) — never content-tiebroken.
  - last_matched: newer date wins.
  - sample_size: numeric MAX (content tiebreak lexically prefers "9" > "26").

Plus the mixed-format id constraint: on-disk ids are sig-001..sig-007 (legacy
3-pad) and sig-8+ (current unpadded allocator), so the id formatter preserves
the OBSERVED form per id instead of re-stamping a uniform width (which would
rename records out from under external references like guard-575's sig-003).

Pure functions — no I/O, no daemon. Governing invariant remains BYTE
commutativity (guard-907): merge(a, b) == merge(b, a) exactly, plus multiround
convergence so the fenced-PUT retry loop terminates.
"""
import json
import sys
from pathlib import Path

import pytest  # noqa: F401 — harness parity with sibling suites

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coordination_merge as cm  # noqa: E402


def _sig(sig_id="sig-33", name="pattern x", created="2026-07-01", **kw):
    rec = {"id": sig_id, "name": name, "created": created,
           "category": "framework-meta", "status": "active",
           "outcome_stats": {"confirmed": 0, "total": 0, "accuracy": 0.0}}
    rec.update(kw)
    return rec


def _blob(recs) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in recs).encode()


def _merged_pair(a_recs, b_recs):
    """Merge both orders; assert byte commutativity; return parsed records."""
    ab = cm.merge_pattern_signatures(_blob(a_recs), _blob(b_recs))
    ba = cm.merge_pattern_signatures(_blob(b_recs), _blob(a_recs))
    assert ab == ba, "merge must stay byte-commutative (guard-907)"
    return [json.loads(ln) for ln in ab.decode().splitlines() if ln.strip()]


# --- union ------------------------------------------------------------------

def test_disjoint_union_keeps_both_sides():
    a = [_sig("sig-8", name="alpha pattern", created="2026-07-01")]
    b = [_sig("sig-9", name="beta pattern", created="2026-07-02")]
    out = _merged_pair(a, b)
    assert sorted(r["id"] for r in out) == ["sig-8", "sig-9"]


# --- key-order byte-commutativity () --------------------------------

def test_distinct_new_keys_byte_commutative_live_probe_shape():
    """The exact live-probe repro from finding
    bravo-fec-idkeyed-keyorder-noncommut-202607161052: base sig-8 record, side
    A adds ``last_matched`` while side B adds ``sample_size``. Pre-fix the
    merged VALUES were identical but key order was dict(a)+b-extras, so
    merge(a,b) != merge(b,a) bytes and the fenced-PUT loop ping-ponged.
    _merged_pair asserts byte commutativity internally."""
    a = [_sig("sig-8", last_matched="2026-07-15")]
    b = [_sig("sig-8", sample_size=9)]
    rec = _merged_pair(a, b)[0]
    assert rec["last_matched"] == "2026-07-15"
    assert rec["sample_size"] == 9


def test_distinct_new_keys_multiround_settles():
    """Once settled, re-merging either stale side against the merged result is
    a byte no-op — the retry loop terminates instead of oscillating."""
    a = [_sig("sig-8", last_matched="2026-07-15")]
    b = [_sig("sig-8", sample_size=9)]
    m = cm.merge_pattern_signatures(_blob(a), _blob(b))
    assert cm.merge_pattern_signatures(m, m) == m
    assert cm.merge_pattern_signatures(m, _blob(b)) == m
    assert cm.merge_pattern_signatures(_blob(a), m) == m


# --- outcome_stats: counter MAX + accuracy recompute --------------------------

def test_outcome_stats_counter_max_with_accuracy_recompute():
    """Divergent counter bumps merge per-counter MAX; accuracy is RECOMPUTED
    from the merged pair (round(c/t, 4), writer parity) — the merged ratio may
    equal NEITHER side's stored accuracy."""
    a = [_sig(outcome_stats={"confirmed": 3, "total": 4, "accuracy": 0.75})]
    b = [_sig(outcome_stats={"confirmed": 2, "total": 5, "accuracy": 0.4})]
    out = _merged_pair(a, b)
    assert len(out) == 1
    stats = out[0]["outcome_stats"]
    assert stats["confirmed"] == 3 and stats["total"] == 5
    assert stats["accuracy"] == 0.6, \
        "accuracy must be recomputed from merged counters, not tiebroken"


def test_outcome_stats_zero_total_recomputes_to_zero():
    a = [_sig(outcome_stats={"confirmed": 0, "total": 0, "accuracy": 0.0})]
    b = [_sig(outcome_stats={"confirmed": 0, "total": 0, "accuracy": 0.31})]
    out = _merged_pair(a, b)
    assert out[0]["outcome_stats"]["accuracy"] == 0.0


# --- status / monotonic fields ------------------------------------------------

def test_retired_dominates_active():
    a = [_sig(status="retired")]
    b = [_sig(status="active",
              outcome_stats={"confirmed": 5, "total": 5, "accuracy": 1.0})]
    out = _merged_pair(a, b)
    assert out[0]["status"] == "retired"
    assert out[0]["outcome_stats"]["confirmed"] == 5  # edits still merge


def test_last_matched_newer_wins():
    a = [_sig(last_matched="2026-07-01")]
    b = [_sig(last_matched="2026-07-10")]
    out = _merged_pair(a, b)
    assert out[0]["last_matched"] == "2026-07-10"


def test_sample_size_numeric_max_not_lexicographic():
    """A bare content tiebreak picks "9" over "26" (lexicographic canon) —
    sample_size must merge as numeric MAX instead."""
    a = [_sig(sample_size=9)]
    b = [_sig(sample_size=26)]
    out = _merged_pair(a, b)
    assert out[0]["sample_size"] == 26


def test_utilization_counter_max():
    a = [_sig(utilization={"times_retrieved": 7, "times_helpful": 1})]
    b = [_sig(utilization={"times_retrieved": 5, "times_helpful": 3})]
    out = _merged_pair(a, b)
    assert out[0]["utilization"] == {"times_retrieved": 7, "times_helpful": 3}


# --- mixed-format id preservation ---------------------------------------------

def test_legacy_padded_id_preserved_byte_exact():
    """sig-003 (legacy 3-pad) must NOT be re-stamped to sig-3 — external
    references (guard-575, weakness-report baselines) key on the on-disk form."""
    a = [_sig("sig-003", name="legacy", created="2026-03-27",
              outcome_stats={"confirmed": 1, "total": 2, "accuracy": 0.5})]
    b = [_sig("sig-003", name="legacy", created="2026-03-27",
              outcome_stats={"confirmed": 2, "total": 2, "accuracy": 1.0})]
    out = _merged_pair(a, b)
    assert out[0]["id"] == "sig-003"


def test_unpadded_id_preserved():
    a = [_sig("sig-8", name="modern", created="2026-06-01")]
    b = [_sig("sig-8", name="modern", created="2026-06-01", sample_size=4)]
    out = _merged_pair(a, b)
    assert out[0]["id"] == "sig-8"


def test_same_int_form_clash_prefers_padded_deterministically():
    """If the two sides somehow carry the SAME id int under different widths
    (sig-007 vs sig-7), the observed-form preference (longer, then lexicographic)
    is symmetric — both orders emit the padded form."""
    a = [_sig("sig-007", name="clash", created="2026-04-01")]
    b = [_sig("sig-7", name="clash", created="2026-04-01")]
    out = _merged_pair(a, b)
    assert len(out) == 1
    assert out[0]["id"] == "sig-007"


# --- concurrent-allocation collision -------------------------------------------

def test_collision_earlier_created_keeps_id_other_displaced_unpadded():
    """Two DISTINCT signatures allocated the same sig-33 concurrently: the
    earlier-created keeps the id; the other is displaced to the next free id in
    the CURRENT allocator's unpadded form."""
    a = [_sig("sig-33", name="first pattern", created="2026-07-01")]
    b = [_sig("sig-33", name="second pattern", created="2026-07-02")]
    out = _merged_pair(a, b)
    by_name = {r["name"]: r["id"] for r in out}
    assert by_name["first pattern"] == "sig-33"
    assert by_name["second pattern"] == "sig-34"


def test_collision_multiround_convergence():
    """Re-merging the merged blob against either input is a fixed point — the
    fenced-PUT retry loop must terminate (re-id'd record recognized by identity,
    never re-duplicated)."""
    a = [_sig("sig-33", name="first pattern", created="2026-07-01")]
    b = [_sig("sig-33", name="second pattern", created="2026-07-02")]
    m1 = cm.merge_pattern_signatures(_blob(a), _blob(b))
    m2 = cm.merge_pattern_signatures(m1, _blob(b))
    m3 = cm.merge_pattern_signatures(m1, _blob(a))
    assert m1 == m2 == m3


def test_counter_merge_multiround_convergence():
    a = [_sig(outcome_stats={"confirmed": 3, "total": 4, "accuracy": 0.75})]
    b = [_sig(outcome_stats={"confirmed": 2, "total": 5, "accuracy": 0.4})]
    m1 = cm.merge_pattern_signatures(_blob(a), _blob(b))
    m2 = cm.merge_pattern_signatures(m1, _blob(b))
    m3 = cm.merge_pattern_signatures(m1, _blob(a))
    assert m1 == m2 == m3


# --- registration ---------------------------------------------------------------

def test_handler_registered_for_basename():
    assert cm.merge_handler_for("pattern-signatures.jsonl") is cm.merge_pattern_signatures


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
