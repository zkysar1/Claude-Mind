"""test_goal_duplication_refusal_remedy.py — .

guard-1470 tells a caller to trim-and-refile before reaching for
--override-duplication, and it carries times_active in the thousands while the
behaviour still drifts. The reason is placement, not awareness: the decision
moment is READING THE REFUSAL, and the refusal used to say only "retitle or
retry with --override-duplication" — two options, no priority, no citation,
and no classification. Worst of all it invited a retitle past a TRUE positive,
because trimming defeats a real overlap exactly as easily as a false one.

These tests pin the remedy the refusal now carries. They exercise EVERY shape,
not just the one the live case happened to produce, because a classifier proved
on one branch is a classifier with three untested branches (guard-2421).

guard-2030 bounds what the remedy may claim: a prescribed classification is a
prescribed PROBE, and a wrong probe "fails toward a FALSE BLOCK". So the last
test asserts the builder reads ONLY fields the emitters actually write, and
`unclassified` is a real branch rather than a guess.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gates.goal_duplication import (  # noqa: E402
    _refusal_remedy,
    _STRONG_STRATEGIES,
    evaluate,
)
import gates.goal_duplication as gd  # noqa: E402


def _failing(name="pending_queue", matches=None):
    return [{"name": name, "passed": False, "reason": "overlap",
             "matches": matches if matches is not None else []}]


# ── identifier-overlap: the TRUE-POSITIVE shape ────────────────────────────
def test_strong_strategy_alone_is_identifier_overlap():
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-115-8418", "match_strategy": "structural_overlap"}]))
    assert r["shape"] == "identifier-overlap"
    assert "STOP and READ" in r["first_action"]
    assert "g-115-8418" in r["first_action"]
    # The load-bearing half: it must forbid the reflex, not merely rank it.
    assert "Do NOT trim" in r["first_action"]


def test_qualified_path_alone_is_identifier_overlap():
    """No strong strategy — a path WITH a directory component carries it."""
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-1-1", "match_strategy": "keyword",
        "file_path_hits": ["core/scripts/precheck_medium_battery.py"]}]))
    assert r["shape"] == "identifier-overlap"
    assert "core/scripts/precheck_medium_battery.py" in r["why"]


def test_goal_id_keyword_hit_alone_is_identifier_overlap():
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-2-2", "match_strategy": "keyword",
        "keyword_hits": ["absent", "g-306-284", "mechanism"]}]))
    assert r["shape"] == "identifier-overlap"
    assert "g-306-284" in r["why"]


@pytest.mark.parametrize("strategy", sorted(_STRONG_STRATEGIES))
def test_every_strong_strategy_routes_to_identifier_overlap(strategy):
    r = _refusal_remedy(_failing(matches=[{"goal_id": "g-3-3",
                                           "match_strategy": strategy}]))
    assert r["shape"] == "identifier-overlap", strategy


# ── basename-only: prose-strength despite a path hit ───────────────────────
def test_bare_basename_is_not_identifier_strength():
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-4-4", "match_strategy": "keyword",
        "file_path_hits": ["SKILL.md"],
        "keyword_hits": ["actual", "available", "because"]}]))
    assert r["shape"] == "basename-only"
    # Do NOT trim here — the basename is usually the goal's own subject.
    assert "Do NOT trim here" in r["first_action"]
    assert "read the match" in r["override_guidance"].lower()


def test_qualified_path_outranks_a_bare_basename():
    """A mixed set is identifier-strength: the qualified path decides."""
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-5-5", "match_strategy": "keyword",
        "file_path_hits": ["SKILL.md", "core/scripts/x.py"]}]))
    assert r["shape"] == "identifier-overlap"


# ── prose-overlap: the FALSE-POSITIVE shape guard-1470 is written for ──────
def test_prose_keywords_only_is_prose_overlap_and_orders_trim_first():
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-6-6", "match_strategy": "keyword",
        "keyword_hits": ["absent", "chain", "observed", "mechanism", "field"],
        "strong_keyword_only": True}]))
    assert r["shape"] == "prose-overlap"
    assert "FIRST trim" in r["first_action"]
    # Override must be named the FALLBACK, with its cost stated.
    assert "only if trimming would lose" in r["override_guidance"]
    assert "AUDITED" in r["override_guidance"]
    assert "fallback, not the peer" in r["override_guidance"]


# ── unclassified: a real branch, not a guess (guard-2030) ──────────────────
def test_no_matches_is_unclassified_not_a_guess():
    r = _refusal_remedy(_failing(name="saturated_frontier", matches=[]))
    assert r["shape"] == "unclassified"
    assert "saturated_frontier" in r["why"]
    assert "Classify by hand" in r["first_action"]


# ── invariants every shape must hold ───────────────────────────────────────
ALL_SHAPES = [
    ([{"goal_id": "a", "match_strategy": "structural_overlap"}], "identifier-overlap"),
    ([{"goal_id": "b", "file_path_hits": ["README.md"]}], "basename-only"),
    ([{"goal_id": "c", "keyword_hits": ["absent", "chain"]}], "prose-overlap"),
    ([], "unclassified"),
]


@pytest.mark.parametrize("matches,shape", ALL_SHAPES)
def test_every_shape_cites_the_guardrail_and_carries_the_caveat(matches, shape):
    r = _refusal_remedy(_failing(matches=matches))
    assert r["shape"] == shape
    assert r["cites"] == "guard-1470"
    # The one caveat no signal can settle must never be dropped.
    assert "VERIFICATION instruction" in r["verification_path_caveat"]
    assert r["first_action"] and r["override_guidance"]


def test_signals_block_reports_only_fields_the_emitters_write():
    """guard-2030: never claim more than the computed signals carry."""
    r = _refusal_remedy(_failing(matches=[{
        "goal_id": "g-7-7", "match_strategy": "title_exact",
        "file_path_hits": ["core/a.py", "b.md"],
        "keyword_hits": ["g-115-42", "prose"]}]))
    sig = r["signals"]
    assert sig["match_strategies"] == ["title_exact"]
    assert sig["qualified_paths"] == ["core/a.py"]
    assert sig["basename_only_paths"] == ["b.md"]
    assert sig["goal_id_hits"] == ["g-115-42"]
    assert sig["matched_goal_ids"] == ["g-7-7"]


# ── wiring: the remedy must reach the surface the caller reads ─────────────
def _stub_checks(monkeypatch, failing_name=None):
    passing = {"passed": True, "reason": "ok", "matches": []}
    for fn in ("_check_recent_completions", "_check_partner_in_flight",
               "_check_git_log", "_check_insight_triggers",
               "_check_target_state", "_check_pending_queue",
               "_check_saturated_frontier"):
        name = fn.lstrip("_").replace("check_", "")
        if name == failing_name:
            monkeypatch.setattr(gd, fn, lambda *a, _n=name, **k: {
                "name": _n, "passed": False, "reason": "overlap with 1 goal",
                "matches": [{"goal_id": "g-9-9",
                             "match_strategy": "structural_overlap"}]})
        else:
            monkeypatch.setattr(gd, fn, lambda *a, _n=name, **k: dict(passing, name=_n))
    monkeypatch.setattr(gd, "_gate_log", lambda *a, **k: None)


def test_blocking_evaluate_puts_the_remedy_in_reason(monkeypatch):
    _stub_checks(monkeypatch, failing_name="pending_queue")
    res = evaluate({"title": "t", "description": "d"}, world_dir=None)
    assert res["would_block"] is True
    assert res["remedy"]["shape"] == "identifier-overlap"
    # reason is the field the caller reads first — the remedy must land THERE,
    # not only in a sibling key nobody prints.
    assert "REMEDY [identifier-overlap, per guard-1470" in res["reason"]
    assert "STOP and READ" in res["reason"]


def test_clean_evaluate_carries_no_remedy(monkeypatch):
    _stub_checks(monkeypatch, failing_name=None)
    res = evaluate({"title": "t", "description": "d"}, world_dir=None)
    assert res["would_block"] is False
    assert "remedy" not in res


def test_remedy_builder_failure_never_refuses_work(monkeypatch):
    """Fail-open: a gate on every filing's critical path must not block on its
    own guidance builder raising (the g-115-7992 precedent)."""
    _stub_checks(monkeypatch, failing_name="pending_queue")
    monkeypatch.setattr(gd, "_refusal_remedy",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res = evaluate({"title": "t", "description": "d"}, world_dir=None)
    assert res["would_block"] is True          # still blocks on the real overlap
    assert res["remedy"] is None               # guidance absent, work not refused
    assert "REMEDY" not in res["reason"]
