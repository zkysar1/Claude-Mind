"""test_retrieve_reach.py — the retrieval-reach fixes of  (2026-09-29).

Each test pins one defect a frozen-snapshot benchmark measured, in the shape
that failed before the fix:

  1. ONE TEXT SURFACE — a guardrail's trigger_condition and a signature's
     name/description/retrieval_cues/indicators were in neither the token
     corpus nor the embedded text. supplementary_text_parts is now the single
     surface both read (retrieve token matcher + embedding-index-build).
  2. UNTAGGED != EVERYWHERE — an entry with no category matched every query
     by category; it now falls through to the text predicate.
  3. UNIVERSAL LESSONS REACH THE DOMAIN LANE — a matching universal
     reasoning-bank entry the 5-slot meta_lessons split does not pick is
     returned in the domain list; one it does pick is never returned twice.
  4. SIGNATURE BLEND — load_pattern_signatures runs the same embedding blend
     as its two sibling lanes.
  5. UNINDEXED TREE NODES — a node absent from the embedding index is scored
     AT the tree cosine floor, not at 0.

Same importlib-against-scratch-world bootstrap as test_embedding_blend.py.
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
_TMPDIR = tempfile.mkdtemp(prefix="retrieve-reach-test-")
os.environ["MIND_WORLD"] = _TMPDIR
os.environ.pop("MIND_AGENT", None)

_spec = importlib.util.spec_from_file_location("retrieve_reach_mod", CORE_SCRIPTS / "retrieve.py")
_retrieve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_retrieve)

if _ORIG_MIND_WORLD is None:
    os.environ.pop("MIND_WORLD", None)
else:
    os.environ["MIND_WORLD"] = _ORIG_MIND_WORLD
if _ORIG_MIND_AGENT is not None:
    os.environ["MIND_AGENT"] = _ORIG_MIND_AGENT

import _embedding_retrieval as er  # noqa: E402

_bspec = importlib.util.spec_from_file_location(
    "embedding_index_build_reach", CORE_SCRIPTS / "embedding-index-build.py")
_build = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(_build)


def _cfg(enabled, min_cos=0.35):
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_blend_enabled"] = enabled
    cfg["embedding_min_cosine"] = min_cos
    return cfg


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    saved = _retrieve._RETRIEVAL_CFG_CACHE
    yield
    _retrieve._RETRIEVAL_CFG_CACHE = saved


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _rb(rid, category, title, util=0.0, applies="specific", **extra):
    rec = {"id": rid, "type": "failure", "status": "active", "category": category,
           "title": title, "content": title, "applies_to": applies,
           "created": "2026-07-01T00:00:00",
           "utilization": {"utilization_score": util, "retrieval_count": 0}}
    rec.update(extra)
    return rec


def _sig(sid, category, name, util=0.0, **extra):
    rec = {"id": sid, "status": "active", "category": category, "name": name,
           "description": name, "created": "2026-07-01T00:00:00",
           "utilization": {"utilization_score": util, "retrieval_count": 0}}
    rec.update(extra)
    return rec


@pytest.fixture()
def stores(tmp_path):
    paths = {"RB_PATH": tmp_path / "reasoning-bank.jsonl",
             "GUARD_PATH": tmp_path / "guardrails.jsonl",
             "SIGS_PATH": tmp_path / "pattern-signatures.jsonl"}
    saved = {k: getattr(_retrieve, k) for k in paths}
    for k, v in paths.items():
        setattr(_retrieve, k, v)
    yield paths
    for k, v in saved.items():
        setattr(_retrieve, k, v)


# ── 1. One text surface ──────────────────────────────────────────────────────

def test_trigger_condition_is_searchable():
    """The field written for the purpose of being found. Before the fix a query
    made only of trigger words reached its guardrail 3.3% of the time."""
    g = {"id": "guard-1", "category": "ops", "rule": "Always drain the queue first.",
         "trigger_condition": "decommissioning a worker pool"}
    assert _retrieve._entry_matches_text(g, ["decommissioning worker pool"])


def test_signature_fields_are_searchable():
    s = {"id": "sig-1", "category": "x", "name": "Stale Premise",
         "description": "an assumption outlived its evidence",
         "retrieval_cues": ["premise drift"], "indicators": ["contradicting measurement"]}
    toks = _retrieve._entry_token_corpus_uncached(s)
    for t in ("stale", "premise", "assumption", "drift", "contradicting"):
        assert t in toks


def test_surface_order_puts_situation_before_body():
    """The encoder truncates at ~126 word-pieces: when-it-applies must come
    before a long body or the body pushes it past the cut."""
    e = {"content": "BODY " * 50, "title": "T", "trigger_condition": "TRIG",
         "when_to_use": {"conditions": ["WHEN"]}, "rule": "RULE"}
    parts = _retrieve.supplementary_text_parts(e)
    assert parts[:3] == ["T", "TRIG", "WHEN"]
    assert parts.index("RULE") < parts.index("BODY " * 50)


def test_when_to_use_both_shapes_and_blank_values_skipped():
    legacy = {"title": "t", "when_to_use": "legacy bare string"}
    canon = {"title": "t", "when_to_use": {"conditions": ["a", "  ", 7, "b"]}}
    assert _retrieve.supplementary_text_parts(legacy) == ["t", "legacy bare string"]
    assert _retrieve.supplementary_text_parts(canon) == ["t", "a", "b"]
    assert _retrieve.supplementary_text_parts({"title": "  ", "rule": ""}) == []


def test_index_builder_embeds_the_same_surface():
    """The two copies of the field list drifted once; there is one now."""
    e = {"title": "T", "trigger_condition": "TRIG", "rule": "RULE",
         "name": "N", "retrieval_cues": ["C"]}
    assert _build.match_text(e) == " ".join(_retrieve.supplementary_text_parts(e))


def test_index_builder_corpus_includes_signatures(stores):
    _write_jsonl(stores["GUARD_PATH"], [])
    _write_jsonl(stores["RB_PATH"], [])
    _write_jsonl(stores["SIGS_PATH"], [
        _sig("sig-1", "c", "Counter Saturation"),
        _sig("sig-2", "c", "Retired One", status="retired")])
    saved = (_build.R.GUARD_PATH, _build.R.RB_PATH, _build.R.SIGS_PATH, _build.R.TREE_PATH)
    _build.R.GUARD_PATH, _build.R.RB_PATH, _build.R.SIGS_PATH = (
        stores["GUARD_PATH"], stores["RB_PATH"], stores["SIGS_PATH"])
    _build.R.TREE_PATH = None
    try:
        docs = [d for d in _build.load_corpus() if d["type"] == "signature"]
    finally:
        (_build.R.GUARD_PATH, _build.R.RB_PATH, _build.R.SIGS_PATH,
         _build.R.TREE_PATH) = saved
    assert [d["id"] for d in docs] == ["sig-1"]
    assert "Counter Saturation" in docs[0]["text"]


# ── 2. Untagged entries ──────────────────────────────────────────────────────

def test_untagged_entry_does_not_match_every_query_by_category():
    assert not _retrieve._entry_matches_category({"category": ""}, ["billing"])
    assert _retrieve._entry_matches_category({"category": ""}, [])


def test_untagged_entry_still_reachable_by_text():
    s = {"id": "sig-9", "category": None, "name": "Single Observation Overgeneralization"}
    assert _retrieve._entry_matches(s, ["single observation overgeneralization"])
    assert not _retrieve._entry_matches(s, ["billing reconciliation"])


# ── 3. Universal lessons in the domain lane ──────────────────────────────────

def test_matching_universal_entry_outside_meta_lessons_reaches_domain(stores):
    """Eight universal entries: the 5-slot split keeps the 5 most-used. The
    query names u7 — the least used — which before the fix was unreachable."""
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)
    recs = [_rb(f"rb-u{i}", f"framework-{i}", f"generic framework lesson {i}",
                util=1.0 - i * 0.1, applies="framework") for i in range(7)]
    recs.append(_rb("rb-u7", "framework-leases", "renewing leases before expiry",
                    util=0.0, applies="framework"))
    _write_jsonl(stores["RB_PATH"], recs)
    domain, universal = _retrieve.load_reasoning_bank(
        ["renewing leases before expiry"], "medium", read_only=True)
    assert "rb-u7" not in {r["id"] for r in universal}
    assert "rb-u7" in {r["id"] for r in domain}


def test_entry_in_meta_lessons_is_never_also_in_domain(stores):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)
    _write_jsonl(stores["RB_PATH"], [
        _rb("rb-u0", "framework-leases", "renewing leases before expiry",
            util=0.9, applies="framework"),
        _rb("rb-d0", "leases", "lease renewal during partitions")])
    domain, universal = _retrieve.load_reasoning_bank(
        ["renewing leases before expiry"], "medium", read_only=True)
    ml = {r["id"] for r in universal}
    assert "rb-u0" in ml
    assert not ml & {r["id"] for r in domain}


def test_dedupe_frees_the_slot_for_the_next_entry(stores):
    """Dedupe runs BEFORE the cap: a universal entry already in meta_lessons
    must not shrink the domain page."""
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(False)
    recs = [_rb("rb-u0", "framework-leases", "leases renewal expiry lesson",
                util=0.9, applies="framework")]
    recs += [_rb(f"rb-d{i}", "leases", f"leases renewal expiry case {i}", util=0.5)
             for i in range(20)]
    _write_jsonl(stores["RB_PATH"], recs)
    domain, universal = _retrieve.load_reasoning_bank(["leases"], "shallow", read_only=True)
    assert "rb-u0" in {r["id"] for r in universal}
    assert len(domain) == _retrieve.SUPPLEMENTARY_CAPS["shallow"]


# ── 4. Signature blend ───────────────────────────────────────────────────────

def test_signature_lane_widens_and_reranks_by_cosine(monkeypatch, stores):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    _write_jsonl(stores["SIGS_PATH"], [
        _sig("sig-hi", "leases", "lease renewal ordering", util=0.9),
        _sig("sig-sem", "detectors", "predicate narrower than its incident class", util=0.0)])
    monkeypatch.setattr(er, "cosine_scores",
                        lambda q, **k: {"sig-sem": 0.8, "sig-hi": 0.4})
    out = _retrieve.load_pattern_signatures(["lease renewal ordering"], "medium",
                                            read_only=True)
    assert [r["id"] for r in out] == ["sig-sem", "sig-hi"]


def test_signature_lane_unchanged_when_no_signature_is_indexed(monkeypatch, stores):
    """An index built before signatures joined it scores none of them: the
    order must be the pre-blend utility order (measured identical)."""
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    _write_jsonl(stores["SIGS_PATH"], [
        _sig("sig-a", "leases", "lease renewal ordering", util=0.2),
        _sig("sig-b", "leases", "lease renewal ordering again", util=0.9)])
    monkeypatch.setattr(er, "cosine_scores", lambda q, **k: {"rb-unrelated": 0.9})
    out = _retrieve.load_pattern_signatures(["leases"], "medium", read_only=True)
    assert [r["id"] for r in out] == ["sig-b", "sig-a"]


def test_signature_lane_as_of_skips_blend(monkeypatch, stores):
    _retrieve._RETRIEVAL_CFG_CACHE = _cfg(True)
    _write_jsonl(stores["SIGS_PATH"], [_sig("sig-a", "leases", "lease renewal")])

    def _boom(*a, **k):
        raise AssertionError("as_of reads must not consult the current index")

    monkeypatch.setattr(er, "cosine_scores", _boom)
    out = _retrieve.load_pattern_signatures(["leases"], "medium", read_only=True,
                                            as_of="2026-09-01T00:00:00")
    assert [r["id"] for r in out] == ["sig-a"]


# ── 5. Unindexed tree nodes ──────────────────────────────────────────────────

def test_unindexed_tree_node_scored_at_floor_not_zero():
    cfg = dict(_retrieve._DEFAULT_RETRIEVAL_CFG)
    cfg["embedding_tree_min_cosine"] = 0.32
    _retrieve._RETRIEVAL_CFG_CACHE = cfg
    node = {"depth": 2, "confidence": 0.5, "summary": "s"}
    matched = [("fresh", dict(node)), ("indexed-weak", dict(node)),
               ("indexed-strong", dict(node))]
    channels = {k: "substring" for k, _ in matched}
    out = _retrieve._score_weight_limit(
        matched, channels, 10, emb_scores={"indexed-weak": 0.10, "indexed-strong": 0.60})
    assert [e[0] for e in out] == ["indexed-strong", "fresh", "indexed-weak"]
    base = {e[0]: e[4] for e in out}
    w = cfg["embedding_cosine_bonus_weight"]
    assert base["fresh"] - base["indexed-weak"] == pytest.approx(w * (0.32 - 0.10), abs=1e-6)

