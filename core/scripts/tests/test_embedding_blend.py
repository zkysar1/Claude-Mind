"""test_embedding_blend.py — unit tests for the supplementary-store
embedding-cosine hybrid (g-306-77 part b2).

Covers retrieve._embedding_blend and its two call sites
(load_reasoning_bank, load_guardrails). The load-bearing invariants:

  1. DEFAULT-OFF NEUTRALITY — with `embedding_blend_enabled` false (the
     shipped default) the blend returns its input unchanged and NEVER
     touches _embedding_retrieval (proven with an exploding stub).
  2. GRACEFUL DEGRADATION — flag on but scores empty (missing index /
     degraded model) → identical to flag-off. cosine_scores raising is
     absorbed by the helper's try/except.
  3. WIDEN — an active entry with zero token overlap joins the candidate
     set when its cosine >= embedding_min_cosine; below-threshold entries
     do not.
  4. RE-RANK — candidates order by cosine desc; entries absent from the
     index sort AT the threshold, preserving their utility order among
     themselves (stable sort).
  5. PARTITION + CAP + BUMP invariants — universal RB entries are never
     widened into the domain list; the depth cap applies after the blend;
     the retrieval_count bump set equals the post-cap return set.
  6. as_of reads skip the blend entirely (historical view must not be
     ranked by current-corpus semantics).

Same bootstrap as test_poignancy_blend.py: retrieve.py imported via
importlib against a scratch MIND_WORLD; loaders run against per-test tmp
JSONL files by pointing the module path globals at them.
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

# Bind retrieve.py to a scratch world before import (it resolves paths from
# MIND_WORLD at module load). Capture/restore so sibling tests don't inherit.
_ORIG_MIND_WORLD = os.environ.get("MIND_WORLD")
_ORIG_MIND_AGENT = os.environ.get("MIND_AGENT")
_TMPDIR = tempfile.mkdtemp(prefix="embedding-blend-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_RETRIEVE_PATH = CORE_SCRIPTS / "retrieve.py"
_spec = importlib.util.spec_from_file_location("retrieve_embblend_mod", _RETRIEVE_PATH)
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

# The blend resolves cosine_scores via `from _embedding_retrieval import
# cosine_scores` inside the function body, so monkeypatching the module
# attribute intercepts every call. Import through the same sys.path.
import _embedding_retrieval as er  # noqa: E402


def _cfg(enabled, min_cos=0.35):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_blend_enabled"] = enabled
    cfg["embedding_min_cosine"] = min_cos
    return cfg


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    """Every test presets _RETRIEVAL_CFG_CACHE explicitly; reset after."""
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


def _rb(rid, category, title, util=0.0, applies="specific", **extra):
    rec = {
        "id": rid, "type": "failure", "status": "active",
        "category": category, "title": title, "content": title,
        "applies_to": applies, "created": "2026-07-01T00:00:00",
        "utilization": {"utilization_score": util, "retrieval_count": 0},
    }
    rec.update(extra)
    return rec


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


@pytest.fixture()
def stores(tmp_path):
    """Point retrieve's RB/GUARD path globals at per-test tmp files."""
    rb_p = tmp_path / "reasoning-bank.jsonl"
    guard_p = tmp_path / "guardrails.jsonl"
    saved_rb, saved_guard = _retrieve.RB_PATH, _retrieve.GUARD_PATH
    _retrieve.RB_PATH, _retrieve.GUARD_PATH = rb_p, guard_p
    yield rb_p, guard_p
    _retrieve.RB_PATH, _retrieve.GUARD_PATH = saved_rb, saved_guard


# Query with no ≥5-char token overlap against the "semantic" records below —
# _entry_matches must be False for them so only the widen pass can add them.
QUERY = ["orchestrating graceful shutdown sequencing"]


# ── 1. Default-off neutrality ────────────────────────────────────────────────

def test_helper_flag_off_returns_input_unchanged(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)

    def _boom(*a, **k):
        raise AssertionError("cosine_scores must not be called when flag is off")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    matched = [_rb("rb-1", "shutdown-sequencing", "graceful shutdown sequencing steps")]
    active = matched + [_rb("rb-2", "colors", "azure crimson palette")]
    out = _retrieve._embedding_blend(matched, active, QUERY)
    assert out is matched  # same object — byte-identical path


def test_loader_flag_off_never_touches_embedding(monkeypatch, stores):
    rb_p, _ = stores
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)

    def _boom(*a, **k):
        raise AssertionError("cosine_scores must not be called when flag is off")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    _write_jsonl(rb_p, [
        _rb("rb-1", "shutdown-sequencing", "graceful shutdown sequencing steps"),
        _rb("rb-2", "colors", "azure crimson palette"),
    ])
    domain, universal = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=True)
    assert [r["id"] for r in domain] == ["rb-1"]
    assert universal == []


# ── 2. Graceful degradation ──────────────────────────────────────────────────

def test_empty_scores_means_token_behavior(monkeypatch, stores):
    rb_p, _ = stores
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {})
    _write_jsonl(rb_p, [
        _rb("rb-1", "shutdown-sequencing", "graceful shutdown sequencing steps"),
        _rb("rb-2", "colors", "azure crimson palette"),
    ])
    domain, _ = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=True)
    assert [r["id"] for r in domain] == ["rb-1"]


def test_raising_scores_degrades_not_raises(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)

    def _boom(*a, **k):
        raise RuntimeError("index corrupt")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    matched = [_rb("rb-1", "shutdown-sequencing", "graceful shutdown")]
    out = _retrieve._embedding_blend(matched, matched, QUERY)
    assert out is matched


# ── 3+4. Widen + re-rank ─────────────────────────────────────────────────────

def test_widen_above_threshold_and_reject_below(monkeypatch, stores):
    rb_p, _ = stores
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True, min_cos=0.35)
    scores = {"rb-1": 0.40, "rb-sem": 0.90, "rb-far": 0.20}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: scores)
    _write_jsonl(rb_p, [
        _rb("rb-1", "shutdown-sequencing", "graceful shutdown sequencing steps"),
        _rb("rb-sem", "lease-renewal", "runner lease expiry vs renewer cadence"),
        _rb("rb-far", "colors", "azure crimson palette"),
    ])
    domain, _ = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=True)
    ids = [r["id"] for r in domain]
    assert ids == ["rb-sem", "rb-1"]  # widened + cosine-desc order
    assert "rb-far" not in ids       # below threshold, no token match


def test_missing_from_index_sorts_at_threshold_keeping_utility_order(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True, min_cos=0.35)
    # Token-matched trio: rb-hi/rb-lo absent from index (post-index adds),
    # rb-weak indexed BELOW threshold. Widened rb-sem indexed at 0.9.
    matched = [
        _rb("rb-hi", "shutdown-sequencing", "shutdown ordering", util=0.9),
        _rb("rb-lo", "shutdown-sequencing", "shutdown ordering redux", util=0.1),
        _rb("rb-weak", "shutdown-sequencing", "shutdown tangent", util=0.5),
    ]
    active = matched + [_rb("rb-sem", "lease-renewal", "lease expiry semantics")]
    scores = {"rb-weak": 0.10, "rb-sem": 0.90}
    import _embedding_retrieval as er_mod
    orig = er_mod.cosine_scores
    er_mod.cosine_scores = lambda q, **k: scores
    try:
        out = _retrieve._embedding_blend(matched, active, QUERY)
    finally:
        er_mod.cosine_scores = orig
    ids = [r["id"] for r in out]
    # cosine order: rb-sem (.9) > rb-hi (.35 default) == rb-lo (.35) > rb-weak (.1)
    # rb-hi before rb-lo — the incoming utility order survives the stable sort.
    assert ids == ["rb-sem", "rb-hi", "rb-lo", "rb-weak"]


# ── 5. Partition, cap, bump invariants ───────────────────────────────────────

def test_universal_rb_never_widened_into_domain(monkeypatch, stores):
    rb_p, _ = stores
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    scores = {"rb-uni": 0.99, "rb-sem": 0.90}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: scores)
    _write_jsonl(rb_p, [
        _rb("rb-uni", "meta-method", "verify before assuming", applies="framework"),
        _rb("rb-sem", "lease-renewal", "lease expiry semantics"),
    ])
    domain, universal = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=True)
    assert [r["id"] for r in domain] == ["rb-sem"]
    assert [r["id"] for r in universal] == ["rb-uni"]  # own partition, once


def test_cap_after_widen_and_bump_set_equals_return_set(monkeypatch, stores):
    _, guard_p = stores
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    # 21 token-matched + 1 high-cosine widened; shallow cap = 20. The widened
    # entry outranks every token match (they're not indexed → threshold), so
    # the return set is rb-sem + 19 token matches; 2 fall off the cap.
    guards = [_rb(f"guard-{i}", "shutdown-sequencing",
                  f"graceful shutdown sequencing rule {i}", util=1.0 - i * 0.01)
              for i in range(21)]
    guards.append(_rb("guard-sem", "lease-renewal", "lease expiry semantics"))
    _write_jsonl(guard_p, guards)
    scores = {"guard-sem": 0.95}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: scores)
    returned = _retrieve.load_guardrails(QUERY, "shallow", read_only=False)
    ids = [r["id"] for r in returned]
    assert len(ids) == 20
    assert ids[0] == "guard-sem"
    # Bump set == return set: re-read the file, only returned ids bumped.
    on_disk = {r["id"]: r for r in map(json.loads, open(guard_p, encoding="utf-8"))}
    for rid, rec in on_disk.items():
        rc = (rec.get("utilization") or {}).get("retrieval_count", 0)
        if rid in ids:
            assert rc == 1, f"{rid} returned but not bumped"
        else:
            assert rc == 0, f"{rid} bumped but not returned"


# ── 5b. Universal relevance split () ─────────────────────────────────

def _uni(rid, util, title="framework lesson"):
    return _rb(rid, f"framework-{rid}", title, util=util, applies="framework")


def test_split_flag_off_is_pure_utilization_slice(monkeypatch):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)

    def _boom(*a, **k):
        raise AssertionError("cosine_scores must not be called when flag is off")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    uni = [_uni(f"rb-u{i}", util=1.0 - i * 0.1) for i in range(8)]
    out = _retrieve._universal_relevance_split(uni, QUERY)
    assert out == uni[:_retrieve.UNIVERSAL_RB_CAP]


def test_split_pulls_relevant_tail_entries_into_cap(monkeypatch, stores):
    rb_p, _ = stores
    cfg = _cfg(True)
    cfg["universal_relevance_slots"] = 2
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    # 8 universal entries, utilization-ordered u0..u7. u6 is the semantic hit
    # (cosine .9) that today's pure-utilization top-5 would never return.
    uni = [_uni(f"rb-u{i}", util=1.0 - i * 0.1) for i in range(8)]
    _write_jsonl(rb_p, uni)
    scores = {"rb-u6": 0.90, "rb-u4": 0.50}
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: scores)
    domain, universal = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=True)
    ids = [r["id"] for r in universal]
    # floor = top-3 by utilization; pulls = u6 (.9) then u4 (.5) by cosine.
    assert ids == ["rb-u0", "rb-u1", "rb-u2", "rb-u6", "rb-u4"]
    assert domain == []


def test_split_backfills_when_no_qualifying_pulls(monkeypatch):
    cfg = _cfg(True)
    cfg["universal_relevance_slots"] = 2
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    uni = [_uni(f"rb-u{i}", util=1.0 - i * 0.1) for i in range(8)]
    # Scores exist (blend engaged) but nothing in the tail clears 0.35.
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {"rb-u5": 0.10})
    out = _retrieve._universal_relevance_split(uni, QUERY)
    assert [r["id"] for r in out] == ["rb-u0", "rb-u1", "rb-u2", "rb-u3", "rb-u4"]


def test_split_partial_pulls_backfill_to_full_cap(monkeypatch):
    cfg = _cfg(True)
    cfg["universal_relevance_slots"] = 2
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    uni = [_uni(f"rb-u{i}", util=1.0 - i * 0.1) for i in range(8)]
    # Only ONE qualifying pull — the second slot must backfill by utilization.
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {"rb-u7": 0.80})
    out = _retrieve._universal_relevance_split(uni, QUERY)
    assert [r["id"] for r in out] == ["rb-u0", "rb-u1", "rb-u2", "rb-u7", "rb-u3"]


def test_split_slots_zero_disables(monkeypatch):
    cfg = _cfg(True)
    cfg["universal_relevance_slots"] = 0
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {"rb-u7": 0.99})
    uni = [_uni(f"rb-u{i}", util=1.0 - i * 0.1) for i in range(8)]
    out = _retrieve._universal_relevance_split(uni, QUERY)
    assert out == uni[:_retrieve.UNIVERSAL_RB_CAP]


def test_split_bump_set_equals_returned_universal(monkeypatch, stores):
    rb_p, _ = stores
    cfg = _cfg(True)
    cfg["universal_relevance_slots"] = 2
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    uni = [_uni(f"rb-u{i}", util=1.0 - i * 0.1) for i in range(8)]
    _write_jsonl(rb_p, uni)
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {"rb-u6": 0.90})
    domain, universal = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=False)
    returned = {r["id"] for r in universal} | {r["id"] for r in domain}
    on_disk = {r["id"]: r for r in map(json.loads, open(rb_p, encoding="utf-8"))}
    for rid, rec in on_disk.items():
        rc = (rec.get("utilization") or {}).get("retrieval_count", 0)
        assert rc == (1 if rid in returned else 0), \
            f"{rid}: rc={rc}, returned={rid in returned}"


# ── 6. as_of skips the blend ─────────────────────────────────────────────────

def test_as_of_read_skips_blend(monkeypatch, stores):
    rb_p, _ = stores
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    called = []
    monkeypatch.setattr(er, "cosine_scores",
                        lambda q, **k: called.append(q) or {"rb-sem": 0.9})
    _write_jsonl(rb_p, [
        _rb("rb-1", "shutdown-sequencing", "graceful shutdown sequencing steps",
            valid_from="2026-06-01T00:00:00"),
        _rb("rb-sem", "lease-renewal", "lease expiry semantics",
            valid_from="2026-06-01T00:00:00"),
    ])
    domain, _ = _retrieve.load_reasoning_bank(
        QUERY, "medium", read_only=True, as_of="2026-07-01T00:00:00")
    assert called == []                      # blend never engaged
    assert [r["id"] for r in domain] == ["rb-1"]  # token-only view
    # Sanity: the same call WITHOUT as_of does engage the blend.
    domain2, _ = _retrieve.load_reasoning_bank(QUERY, "medium", read_only=True)
    assert called and [r["id"] for r in domain2] == ["rb-sem", "rb-1"]
