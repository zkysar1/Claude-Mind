#!/usr/bin/env python3
# domain-leak-exempt: tests the Gate D experiment seam (_gate_d.py) — references
# the GATE_D_* flag namespace and the gate-d-v1 experiment constants by design.
"""Tests for _gate_d.py — Gate D commons-pattern injection assignment + retrieval.

Pins the pre-registered FROZEN constants (GATE-INTEGRITY, methodology 9.5 — a
change voids the experiment) and verifies the FNV-1a salted-parity assignment,
the Gate-C-parity tokenizer, top-K=5 selection, the 2000-token cap, and the
three terminal statuses (control / no_patterns / injected). Binding spec: Gate D
methodology §2 / §4.5 / §9.3 + Appendix B (RATIFIED 2026-06-10).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import _gate_d  # noqa: E402 — conftest puts core/scripts on sys.path

GATE_D_PY = Path(_gate_d.__file__)


# ── Frozen constants (Appendix B) — a change here voids the experiment ──────
def test_frozen_constants():
    assert _gate_d.GATE_D_SALT == 0x47443034          # ASCII "GD04"
    assert _gate_d.FNV_OFFSET_BASIS == 0x811C9DC5
    assert _gate_d.FNV_PRIME == 0x01000193
    assert _gate_d.TOP_K == 5
    assert _gate_d.TOKEN_CAP == 2000
    assert _gate_d.EXPERIMENT_VERSION == "gate-d-v1"


# ── FNV-1a algorithm correctness (independent of the salt) ──────────────────
def _fnv1a_32(s):
    h = _gate_d.FNV_OFFSET_BASIS
    for c in s.encode("utf-8"):
        h ^= c
        h = (h * _gate_d.FNV_PRIME) & 0xFFFFFFFF
    return h


def test_fnv1a_known_vectors():
    # Canonical FNV-1a 32-bit test vectors.
    assert _fnv1a_32("") == 0x811C9DC5
    assert _fnv1a_32("a") == 0xE40C292C
    assert _fnv1a_32("foobar") == 0xBF9CF968


# ── assign_arm: determinism, parity, input construction, regression ─────────
def test_assign_arm_deterministic():
    assert _gate_d.assign_arm("g-325-07") == _gate_d.assign_arm("g-325-07")


def test_assign_arm_parity():
    # arm == 'A' iff the 32-bit hash is even (parity bit 0).
    for i in range(200):
        arm, h = _gate_d.assign_arm(f"g-parity-{i}")
        assert arm == ("A" if (h & 1) == 0 else "B")


def test_assign_arm_input_construction():
    # input = goalId + ':' + hex(salt without 0x) -> goalId + ':47443034'.
    _, h = _gate_d.assign_arm("g-325-07")
    assert h == _fnv1a_32("g-325-07:47443034")


def test_assign_arm_regression_anchors():
    # Pins the smoke-tested values — any constant/algorithm drift breaks these.
    assert _gate_d.assign_arm("g-325-07") == ("A", 0x4DC209E2)
    assert _gate_d.assign_arm("g-115-744")[0] == "B"
    assert _gate_d.assign_arm("g-001-12")[0] == "B"


def test_assign_arm_distribution_roughly_balanced():
    arms = [_gate_d.assign_arm(f"g-dist-{i}")[0] for i in range(1000)]
    assert 400 <= arms.count("B") <= 600


# ── tokenize: lowercase, 3+ char, stopword removal ──────────────────────────
def test_tokenize_basic():
    toks = _gate_d.tokenize("The Quick Brown Fox jumps")
    assert "the" not in toks                       # stopword
    assert {"quick", "brown", "fox", "jumps"} <= toks


def test_tokenize_min_length_and_stopwords():
    assert _gate_d.tokenize("a an it of to the") == set()   # <3 char or stopword
    assert _gate_d.tokenize("") == set()
    assert _gate_d.tokenize("API api API") == {"api"}       # lowercased + deduped


# ── helpers ─────────────────────────────────────────────────────────────────
def _find_arm(target, prefix="g-find"):
    for i in range(2000):
        gid = f"{prefix}-{i}"
        if _gate_d.assign_arm(gid)[0] == target:
            return gid
    raise RuntimeError(f"no {target} id found in 2000 candidates")


def _run(gid, goal_text="", category="", corpus_path=None):
    env = dict(os.environ)
    if corpus_path is None:
        env.pop("GATE_D_CORPUS_PATH", None)
    else:
        env["GATE_D_CORPUS_PATH"] = str(corpus_path)
    out = subprocess.run(
        [sys.executable, str(GATE_D_PY), "--goal-id", gid,
         "--goal-text", goal_text, "--category", category],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    return json.loads(out.stdout)


# ── end-to-end terminal statuses ────────────────────────────────────────────
def test_arm_a_is_control_noop():
    r = _run(_find_arm("A"), "anything at all", "framework")
    assert r["arm"] == "A"
    assert r["status"] == "control"
    assert r["patterns"] == []
    assert r["injection_tokens"] == 0


def test_arm_b_no_corpus_is_no_patterns():
    r = _run(_find_arm("B"), "database migration schema", "framework")
    assert r["arm"] == "B"
    assert r["status"] == "no_patterns"
    assert r["patterns"] == []


def test_arm_b_injection_top_k(tmp_path):
    # Launch-inventory schema (amendment 3): injectable content lives under
    # rec["generalized"]; signature is world:kind:id; approach may be a list.
    corpus = tmp_path / "corpus.jsonl"
    lines = [json.dumps({
        "world": "test", "kind": "rb", "id": f"rb-{i:03d}",
        "generalized": {
            "context": "database migration schema change rollout",
            "approach": ["backfill then atomic swap"],
            "lesson": "verify symmetry before applying a clamp",
        },
    }) for i in range(8)]
    lines.append(json.dumps({                       # zero-overlap distractor
        "world": "test", "kind": "rb", "id": "rb-zzz",
        "generalized": {"context": "cooking recipes simmer",
                        "approach": ["taste often"], "lesson": "salt at the end"},
    }))
    corpus.write_text("\n".join(lines), encoding="utf-8")
    r = _run(_find_arm("B"), "database migration schema rollout", "framework", corpus)
    assert r["arm"] == "B"
    assert r["status"] == "injected"
    assert 1 <= r["patterns_injected"] <= _gate_d.TOP_K
    assert 0.0 <= r["retrieval_precision"] <= 1.0
    assert len(r["pattern_signatures"]) == r["patterns_injected"]
    assert "test:rb:rb-zzz" not in r["pattern_signatures"]   # zero-overlap excluded
    # each injected pattern carries the documented shape
    for p in r["patterns"]:
        assert set(p) >= {"context", "approach", "lesson", "signature"}


def test_arm_b_token_cap(tmp_path):
    corpus = tmp_path / "corpus_big.jsonl"
    big = "schema " * 100                            # ~525 approx-tokens/pattern
    lines = [json.dumps({
        "world": "test", "kind": "rb", "id": f"big-{i:03d}",
        "generalized": {"context": big, "approach": [big], "lesson": big},
    }) for i in range(8)]
    corpus.write_text("\n".join(lines), encoding="utf-8")
    r = _run(_find_arm("B"), "schema schema schema", "framework", corpus)
    assert r["status"] == "injected"
    assert r["injection_tokens"] <= _gate_d.TOKEN_CAP
    assert 1 <= r["patterns_injected"] < _gate_d.TOP_K   # cap bites before top-K


def test_arm_b_original_fields_never_inject(tmp_path):
    # Amendment 3 invariant: top-level original.* is zds-verbatim and MUST NOT
    # inject — a record whose only matching text sits under "original" (or at
    # the legacy top level) yields no_patterns.
    corpus = tmp_path / "corpus_orig.jsonl"
    corpus.write_text(json.dumps({
        "world": "test", "kind": "guardrail", "id": "guard-001",
        "original": {"context": "database migration schema rollout",
                     "approach": "backfill then atomic swap",
                     "lesson": "verify symmetry"},
        "context": "database migration schema rollout",
        "generalized": {"context": "", "approach": [], "lesson": ""},
    }), encoding="utf-8")
    r = _run(_find_arm("B"), "database migration schema rollout", "framework", corpus)
    assert r["status"] == "no_patterns"
    assert r["patterns_injected"] == 0


def test_bad_corpus_path_no_crash(tmp_path):
    r = _run(_find_arm("B"), "x", "y", tmp_path / "does-not-exist.jsonl")
    assert r["arm"] == "B"
    assert r["status"] == "no_patterns"                  # missing file -> empty corpus
