#!/usr/bin/env python3
"""Tests for commons-retrieve.py ().

Covers the things that would silently break the feature:
  1. score_patterns — the token-overlap matcher. The measured reason this exists
     instead of ?tag=<goal.category> is that the two vocabularies are largely
     disjoint (31% vs 72% coverage on the live queue, 2026-07-26). A regression
     to exact-tag semantics would look like a working feature while retrieving
     nothing for most goals.
  2. _merge_manifest — must ADD `commons_patterns` without disturbing any key
     retrieve.sh wrote. supplementary_detail in particular drives utilization
     counters keyed by LOCAL record id; clobbering or polluting it corrupts
     utilization-feedback.
  3. the DEGRADED write path — the bare-write fallback must report distinctly,
     or a persistent locked-write failure looks exactly like success
     (fresh-eyes-code finding, g-335-211).
"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _paths  # noqa: E402

# The script under test lives in world/scripts, not core/scripts: it speaks ONE
# product's gateway, so it is domain-specific by construction. Core owns only the
# `commons-retrieval` hook slot (execute-protocol-digest Step 4a); the world owns
# the implementation. See core/config/conventions/domain-hooks.md.
_TARGET = Path(_paths.WORLD_DIR) / "scripts" / "commons-retrieve.py"
if not _TARGET.exists():                                  # pragma: no cover
    import pytest
    pytest.skip(f"domain hook not installed in this world: {_TARGET}",
                allow_module_level=True)

_spec = importlib.util.spec_from_file_location("commons_retrieve", _TARGET)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)


def _p(sig, tags, retrievals=0):
    return {"signature": sig, "tags": tags, "tier": "shared",
            "maturity": "stable", "retrievalCount": retrievals}


def test_tokens_drops_fillers_and_short_words():
    t = cr._tokens("The a of Error-Handling in AWS")
    assert "error" in t and "handling" in t and "aws" in t
    assert "the" not in t and "of" not in t and "in" not in t


def test_scores_and_ranks_by_overlap_then_retrievals():
    pats = [
        _p("a", ["error-handling", "diagnostics"], retrievals=1),
        _p("b", ["error-handling", "diagnostics", "verification"], retrievals=0),
        _p("c", ["unrelated-topic", "gardening"], retrievals=99),
    ]
    q = cr._tokens("error handling") | cr._tokens("diagnostics verification pass")
    out = cr.score_patterns(pats, q, min_overlap=2)
    assert [r["signature"] for r in out] == ["b", "a"], out
    # 4, not 3: overlap counts TOKENS, and 'error-handling' splits into two.
    assert out[0]["overlap_count"] == 4
    assert set(out[0]["overlap_tokens"]) == {
        "error", "handling", "diagnostics", "verification"}
    # 'c' has zero overlap and must not ride in on its huge retrievalCount
    assert all(r["signature"] != "c" for r in out)


def test_min_overlap_is_enforced():
    pats = [_p("a", ["error-handling"])]
    q = cr._tokens("error only")
    assert cr.score_patterns(pats, q, min_overlap=2) == []
    assert len(cr.score_patterns(pats, q, min_overlap=1)) == 1


def test_dedupes_by_signature():
    """The live commons returns duplicate rows per signature (200 rows / 107
    distinct, g-335-242). Without dedupe a doubled pattern occupies two
    --draw-top slots and gets PAID for twice."""
    pats = [_p("dup", ["error-handling", "diagnostics"]),
            _p("dup", ["error-handling", "diagnostics"])]
    out = cr.score_patterns(pats, cr._tokens("error handling diagnostics"), 2)
    assert len(out) == 1


def test_exact_tag_semantics_would_undermatch():
    """Guards the DESIGN, not just the code: a goal whose category is absent
    from the tag vocabulary must still match on title tokens. If someone
    'simplifies' this back to tag==category, this test fails."""
    pats = [_p("a", ["error-handling", "verification"])]
    # category 'framework-hygiene' is NOT a commons tag; the title carries the signal
    q = cr._tokens("framework-hygiene") | cr._tokens("error handling verification sweep")
    assert len(cr.score_patterns(pats, q, min_overlap=2)) == 1


def test_merge_manifest_preserves_existing_keys(tmp_path):
    sess = tmp_path / "session"
    sess.mkdir()
    prior = {
        "schema_version": 3, "goal_id": "g-1",
        "supplementary_detail": [{"id": "rb-1", "type": "reasoning_bank"}],
        "tree_nodes_loaded": ["node-a"], "counts": {"tree_nodes": 1},
        "utilization_pending": True,
    }
    (sess / "retrieval-session.json").write_text(json.dumps(prior), encoding="utf-8")

    assert cr._merge_manifest(tmp_path, {"verdict": "ok", "matched": 3}) == "written"

    after = json.loads((sess / "retrieval-session.json").read_text(encoding="utf-8"))
    assert after["commons_patterns"]["matched"] == 3
    for k, v in prior.items():
        assert after[k] == v, f"{k} was disturbed by the commons merge"


def test_merge_manifest_creates_file_when_absent(tmp_path):
    assert cr._merge_manifest(tmp_path, {"verdict": "ok"}) == "written"
    after = json.loads(
        (tmp_path / "session" / "retrieval-session.json").read_text(encoding="utf-8"))
    assert after["commons_patterns"]["verdict"] == "ok"


def test_merge_manifest_survives_corrupt_prior(tmp_path):
    """A malformed manifest must not make the step throw — fail-open."""
    sess = tmp_path / "session"
    sess.mkdir()
    (sess / "retrieval-session.json").write_text("{not json", encoding="utf-8")
    assert cr._merge_manifest(tmp_path, {"verdict": "ok"}) == "written"


def test_degraded_write_reports_distinctly(tmp_path, monkeypatch):
    """The bare-write fallback must NOT report plain 'written'.

    locked_write_json can raise (its docstring: under own-cloud a stale If-Match
    fence yields conflict_error, deliberately not retried), so the fallback is
    reachable in normal operation. It writes with no lock, no history snapshot
    and no changelog entry — a persistent degradation there must be visible, not
    silently indistinguishable from the audited path (guard-946 class).
    """
    import _fileops
    monkeypatch.setattr(_fileops, "locked_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    status = cr._merge_manifest(tmp_path, {"verdict": "ok"})
    assert status.startswith("written:unlocked-fallback:"), status
    assert "RuntimeError" in status
    # fail-open still holds: the record IS on disk despite the degraded path
    after = json.loads(
        (tmp_path / "session" / "retrieval-session.json").read_text(encoding="utf-8"))
    assert after["commons_patterns"]["verdict"] == "ok"


def test_env_value_never_calls_the_secrets_dict(monkeypatch):
    """guard-1461: _paths._env_local_vars is a DICT. Calling it raises a
    TypeError whose message serializes EVERY secret on the box into the
    transcript — that is exactly how the 2026-07-26 exposure happened, and the
    swallowed TypeError also made the script report no_credentials. Subscript,
    never call."""
    import _paths
    assert isinstance(_paths._env_local_vars, dict), \
        "_env_local_vars must stay a dict; if it becomes callable, revisit guard-1461"
    monkeypatch.setenv("COMMONS_TEST_KEY", "value-from-env")
    assert cr._env_value("COMMONS_TEST_KEY") == "value-from-env"
    assert cr._env_value("DEFINITELY_NOT_SET_ANYWHERE_XYZ") == ""


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-v"]))
