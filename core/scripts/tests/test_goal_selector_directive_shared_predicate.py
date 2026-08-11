"""test_goal_selector_directive_shared_predicate.py -- .

Pins that the SCORING path (load_active_directives -> directive_boost_score)
and the guard-1310 MUST-SELECT banner (emit_directive_honor_banner) admit the
SAME directive set, because both delegate to ONE predicate --
parse_directive_admission.

THE DEFECT THIS PINS (measured on the live coordination board, 32 directive
messages): the two call sites decided admission independently and disagreed on
exactly one clause. Scoring required an explicit `weight:` tag
(`if weight == 0.0: continue`); the banner required only `target:` tags plus
directed-at-this-agent and never looked at weight. So the banner fired a
MUST-SELECT imperative for directives the scorer had scored at 0.00 --
observed naming g-356-03/05/06/09/10 while all five ranked #405-#418 of 422.
2 of 32 carried `weight:` (6.2%), 18 carried targets (56.2%), exactly 1 carried
both and could therefore score. Of the 2 with `weight:`, one was `weight:high`
-> float() raised -> bare except swallowed it -> weight stayed 0.0, silently,
with a tag present that LOOKED correct.

WHAT WOULD HAVE CAUGHT IT, AND WHAT WOULD NOT. A test that exercised either
path ALONE passes in the defective state -- each function was individually
self-consistent, which is why this defect survived a suite that already had 12
tests pinning the banner. The bug lived in the RELATIONSHIP between the two.
So every agreement test below drives BOTH functions over the SAME fixture and
asserts on the pair; a single-function assertion here would be theatre.

Daemon-safe (no daemon_integration marker): board fixtures are tmp files and
the scoring path is driven through the module-level cache, so the real
coordination board is never read and no daemon spawns.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(alias, filename):
    path = CORE_SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load("goal_selector_dsp", "goal-selector.py")


def _board(tmp_path, rows):
    p = tmp_path / "coordination.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _scored(*goal_ids):
    return [{"goal_id": g, "score": round(10.0 - i, 2)} for i, g in enumerate(goal_ids)]


def _directive(mid, tags, text="do the thing"):
    return {"id": mid, "type": "directive", "author": "alpha",
            "text": text, "tags": tags}


def _scoring_admits(monkeypatch, board, goal_id, category):
    """Drive the SCORING path over `board` and return the boost for one goal."""
    monkeypatch.setattr(gs, "BOARD_COORD_PATH", board)
    monkeypatch.setattr(gs, "_ACTIVE_DIRECTIVES", None)  # defeat the run cache
    return gs.directive_boost_score(goal_id, category)


# --- the core contract: both paths, one predicate ---------------------------

def test_weightless_directive_is_admitted_by_BOTH_paths(tmp_path, monkeypatch):
    """The regression case. Pre-fix: banner fired, scoring returned 0.0."""
    board = _board(tmp_path, [_directive("d-weightless", ["foxtrot", "target:g-1"])])

    boost = _scoring_admits(monkeypatch, board, "g-1", "any-cat")
    warns = gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board)

    assert boost == gs.DIRECTIVE_DEFAULT_WEIGHT, (
        "weightless targeted directive must now SCORE at the default weight; "
        "returning 0.0 here is the exact pre-fix defect")
    assert [w["directive_id"] for w in warns] == ["d-weightless"]
    # The pair is the point: banner fired AND the scorer backed it numerically.
    assert boost > 0.0 and warns


def test_weighted_directive_is_admitted_by_BOTH_paths(tmp_path, monkeypatch):
    """The check's 'one weighted fixture' half -- explicit weight is preserved."""
    board = _board(tmp_path, [_directive("d-weighted", ["foxtrot", "target:g-1", "weight:2.5"])])

    boost = _scoring_admits(monkeypatch, board, "g-1", "any-cat")
    warns = gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board)

    assert boost == 2.5, "an explicit weight must survive unchanged, not be defaulted"
    assert [w["directive_id"] for w in warns] == ["d-weighted"]


def test_both_paths_agree_on_a_mixed_board(tmp_path, monkeypatch):
    """Agreement over a board mixing weightless / weighted / expired / untargeted.

    Asserts SET EQUALITY between what scoring admits and what the banner
    considers -- the relationship the defect broke.
    """
    rows = [
        _directive("d-weightless", ["foxtrot", "target:g-1"]),
        _directive("d-weighted", ["foxtrot", "target:g-2", "weight:2.0"]),
        _directive("d-expired", ["foxtrot", "target:g-3", "expires:2000-01-01T00:00:00"]),
        _directive("d-untargeted", ["foxtrot", "weight:5.0"]),
    ]
    board = _board(tmp_path, rows)
    monkeypatch.setattr(gs, "BOARD_COORD_PATH", board)
    monkeypatch.setattr(gs, "_ACTIVE_DIRECTIVES", None)

    scoring_targets = {g for d in gs.load_active_directives() for g in d["target_goals"]}
    banner_targets = {w["goal_id"] for w in gs.emit_directive_honor_banner(
        _scored("g-1", "g-2", "g-3"), "foxtrot", board_path=board)}

    assert scoring_targets == banner_targets == {"g-1", "g-2"}, (
        f"scoring={scoring_targets} banner={banner_targets} -- the two paths "
        "must admit the same directives; divergence here is the g-115-4639 defect")


# --- outcome 3: a non-numeric weight fails LOUD, not silently ---------------

def test_non_numeric_weight_warns_on_stderr_naming_the_directive(tmp_path, monkeypatch, capsys):
    board = _board(tmp_path, [_directive("d-bad-weight", ["foxtrot", "target:g-1", "weight:high"])])

    boost = _scoring_admits(monkeypatch, board, "g-1", "any-cat")
    err = capsys.readouterr().err

    assert "d-bad-weight" in err, "the warning must NAME the directive id"
    assert "weight:high" in err, "the warning must quote the offending tag"
    assert boost == gs.DIRECTIVE_DEFAULT_WEIGHT, (
        "a typo in ONE tag must not silently discard the whole directive")


def test_valid_weight_does_not_warn(tmp_path, monkeypatch, capsys):
    """Negative control -- without this, the test above passes on a script that
    warns unconditionally."""
    board = _board(tmp_path, [_directive("d-ok", ["foxtrot", "target:g-1", "weight:2.0"])])
    _scoring_admits(monkeypatch, board, "g-1", "any-cat")
    assert "non-numeric" not in capsys.readouterr().err


# --- unchanged semantics the shared predicate must not have broken ----------

def test_expired_directive_admitted_by_neither(tmp_path, monkeypatch):
    board = _board(tmp_path, [
        _directive("d-expired", ["foxtrot", "target:g-1", "expires:2000-01-01T00:00:00"])])
    assert _scoring_admits(monkeypatch, board, "g-1", "any-cat") == 0.0
    assert gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board) == []


def test_untargeted_directive_admitted_by_neither(tmp_path, monkeypatch):
    board = _board(tmp_path, [_directive("d-none", ["foxtrot", "weight:9.0"])])
    assert _scoring_admits(monkeypatch, board, "g-1", "any-cat") == 0.0
    assert gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board) == []


def test_category_targeting_still_scores(tmp_path, monkeypatch):
    """category: targets reach scoring (the banner is goal-id-only by design,
    so this is deliberately NOT an agreement assertion)."""
    board = _board(tmp_path, [_directive("d-cat", ["foxtrot", "category:lodestar"])])
    assert _scoring_admits(monkeypatch, board, "g-1", "lodestar") == gs.DIRECTIVE_DEFAULT_WEIGHT
    assert _scoring_admits(monkeypatch, board, "g-1", "other-cat") == 0.0


def test_non_directive_rows_are_ignored(tmp_path, monkeypatch):
    board = _board(tmp_path, [
        {"id": "s-1", "type": "status", "author": "alpha", "text": "hi",
         "tags": ["foxtrot", "target:g-1", "weight:9.0"]}])
    assert _scoring_admits(monkeypatch, board, "g-1", "any-cat") == 0.0
    assert gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board) == []


# --- the weight-sign invariant (found by the  fresh-eyes pass) ----
#
# THE HOLE THESE CLOSE. The first cut of this file had 10 tests and none used a
# zero or negative weight, so it certified "the two paths agree" while an
# explicit `weight:0` was admitted, scored 0.00, and STILL fired the MUST-SELECT
# banner -- reproducing the exact defect the fix had just closed. The original
# `if weight == 0.0: continue` had covered that case BY ACCIDENT; removing it to
# fix the implicit-zero path silently opened the explicit one. The lesson is the
# boundary value: a predicate rewritten around a condition must be re-tested AT
# that condition, not only on the side that motivated the rewrite.

def test_explicit_zero_weight_is_dropped_by_BOTH_paths(tmp_path, monkeypatch):
    """`weight:0` is the author disabling the directive -- obey it everywhere."""
    board = _board(tmp_path, [_directive("d-zero", ["foxtrot", "target:g-1", "weight:0"])])

    boost = _scoring_admits(monkeypatch, board, "g-1", "any-cat")
    warns = gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board)

    assert boost == 0.0
    assert warns == [], (
        "an explicitly zero-weighted directive must NOT fire a MUST-SELECT "
        "banner -- that is the g-115-4639 defect surviving in the explicit case")


def test_negative_weight_scores_but_does_not_compel(tmp_path, monkeypatch):
    """A negative weight is a DEPRIORITISATION. Scoring applies it; a banner
    ordering immediate selection would contradict the author."""
    board = _board(tmp_path, [_directive("d-neg", ["foxtrot", "target:g-1", "weight:-2.0"])])

    boost = _scoring_admits(monkeypatch, board, "g-1", "any-cat")
    warns = gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board)

    assert boost == -2.0, "the negative bias must still reach scoring"
    assert warns == [], "a deprioritising directive must not compel selection"


def test_banner_fires_iff_boost_is_positive(tmp_path, monkeypatch):
    """The invariant, stated once over the whole weight domain. A table test
    here is what a per-case test set kept missing at the boundary."""
    cases = [
        (["weight:0"], 0.0, False),
        (["weight:-2.0"], -2.0, False),
        ([], gs.DIRECTIVE_DEFAULT_WEIGHT, True),      # weightless -> default
        (["weight:2.0"], 2.0, True),
        (["weight:high"], gs.DIRECTIVE_DEFAULT_WEIGHT, True),  # typo -> default
    ]
    for extra, expected_boost, expect_banner in cases:
        board = _board(tmp_path, [_directive("d-c", ["foxtrot", "target:g-1"] + extra)])
        boost = _scoring_admits(monkeypatch, board, "g-1", "any-cat")
        warns = gs.emit_directive_honor_banner(_scored("g-1"), "foxtrot", board_path=board)
        assert boost == expected_boost, f"{extra}: boost {boost} != {expected_boost}"
        assert bool(warns) is expect_banner, f"{extra}: banner {bool(warns)} != {expect_banner}"
        assert (boost > 0) == bool(warns), (
            f"{extra}: INVARIANT BROKEN -- the banner must fire iff the boost "
            "is positive, or the banner is compelling a selection the scorer "
            "does not support")


def test_typo_weight_defaults_but_explicit_zero_drops(tmp_path):
    """The asymmetry is deliberate: `weight:high` is a TYPO (intent unknown ->
    preserve the directive at the default), `weight:0` is an INSTRUCTION (intent
    stated -> obey it). Pinned so a later 'simplification' cannot collapse them."""
    typo = gs.parse_directive_admission(_directive("d-t", ["target:g-1", "weight:high"]))
    zero = gs.parse_directive_admission(_directive("d-z", ["target:g-1", "weight:0"]))
    assert typo is not None and typo["weight"] == gs.DIRECTIVE_DEFAULT_WEIGHT
    assert zero is None


def test_parse_directive_admission_reports_weight_explicit(tmp_path):
    """weight_explicit distinguishes 'author stated it' from 'we defaulted it' --
    the field a future caller needs to tell a deliberate weight from a fallback."""
    explicit = gs.parse_directive_admission(
        _directive("d-a", ["target:g-1", "weight:2.0"]))
    defaulted = gs.parse_directive_admission(
        _directive("d-b", ["target:g-1"]))
    bad = gs.parse_directive_admission(
        _directive("d-c", ["target:g-1", "weight:high"]))

    assert (explicit["weight"], explicit["weight_explicit"]) == (2.0, True)
    assert (defaulted["weight"], defaulted["weight_explicit"]) == (gs.DIRECTIVE_DEFAULT_WEIGHT, False)
    # A bad weight is a FALLBACK, not an author-stated weight.
    assert (bad["weight"], bad["weight_explicit"]) == (gs.DIRECTIVE_DEFAULT_WEIGHT, False)
