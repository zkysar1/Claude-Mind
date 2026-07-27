"""Regression tests for the  distinctive-token discrimination fix.

WHAT WAS BROKEN. `retrieve._distinctive_tokens` applied a 31-word stopword list
and kept the first 40 survivors IN DOCUMENT ORDER — no IDF, no rarity, no shape
test. It emitted `all, must, never, two, first, when, they, only, same, where`,
and `utilization-feedback.infer_feedback.classify` marked an item helpful when
just ONE of those appeared anywhere in a multi-KB goal description.

MEASURED (g-115-3134, 6 real manifests / 485 items): mean helpful/population
0.922 at min_distinctive=1 and 0.822 at =2 — tuning the threshold did not fix
it. The decisive evidence was a NEGATIVE CONTROL: an unrelated CAKE RECIPE
scored 301/480 (0.627) against those same manifests, so 68% of every "helpful"
verdict was reproducible by topically-unrelated text. A 9-char text scored
0.143 — the output tracked goal-text LENGTH, not content.

THE FIX (two halves, both required):
  producer — `_TOKEN_RE` keeps identifiers whole (`movement-navigation` is ONE
             token, not {movement, navigation}), and structural tokens are
             ranked ahead of prose BEFORE the 40-cap.
  consumer — `classify` counts only STRUCTURAL overlap (rb-1729: token SHAPE is
             the discriminator, not generic prose vocab).
Neither half works alone: the old tokenizer destroyed shape before comparison,
so a shape rule bolted onto `classify` alone measured 0.02 (verified).

The load-bearing test here is `test_negative_control_scores_zero`. Saturation
is unfalsifiable from the positive side — "everything is relevant" and "the
test is degenerate" look identical — so the control is what makes the property
checkable at all (rb-5125, guard-1432).
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "core" / "scripts"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


retrieve = _load("retrieve", _ROOT / "core" / "scripts" / "retrieve.py")


# --- producer: identifier shape ------------------------------------------

@pytest.mark.parametrize("ident", [
    "movement-navigation", "loop_state", "rb-1729", "g-115-3144",
    "utilization-score-semantics",
])
def test_identifiers_survive_whole(ident):
    """The old [a-z0-9]+ tokenizer split these; nothing downstream could then
    apply a shape test, because no token carried shape."""
    assert ident in retrieve._distinctive_tokens(f"see the {ident} entry")


@pytest.mark.parametrize("tok,expected", [
    ("rb-1729", True), ("movement-navigation", True), ("loop_state", True),
    ("g-115-3144", True), ("2026", True),
    ("architecture", False), ("framework", False), ("never", False),
    ("first", False), ("all", False),
    ("a-b", False),  # len<=3 — too short to be a meaningful identifier
])
def test_structural_test_separates_identifiers_from_prose(tok, expected):
    assert retrieve._is_structural_token(tok) is expected


def test_structural_tokens_rank_before_prose():
    """The cap must keep the informative tokens, not whatever appeared first.

    The old version sliced the first 40 in DOCUMENT ORDER, so prose that
    happened to lead the text crowded out identifiers appearing later.
    """
    text = ("alpha beta gamma delta epsilon zeta eta theta iota kappa "
            "lambda mu nu xi omicron pi rho sigma tau upsilon "
            "and finally the rb-1729 entry plus loop_state")
    toks = retrieve._distinctive_tokens(text)
    structural = [t for t in toks if retrieve._is_structural_token(t)]
    assert "rb-1729" in structural and "loop_state" in structural
    # every structural token precedes every prose token
    first_prose = next(i for i, t in enumerate(toks)
                       if not retrieve._is_structural_token(t))
    last_structural = max(i for i, t in enumerate(toks)
                          if retrieve._is_structural_token(t))
    assert last_structural < first_prose


def test_cap_and_empty_input_hold():
    assert retrieve._distinctive_tokens("") == []
    assert retrieve._distinctive_tokens(None) == []
    many = " ".join(f"ident-{i}" for i in range(200))
    assert len(retrieve._distinctive_tokens(many)) == \
        retrieve._MAX_DISTINCTIVE_TOKENS


# --- consumer: the classify contract --------------------------------------
# classify() is a closure inside infer_feedback, so these reproduce its exact
# predicate against the producer's real output rather than importing it.

CAKE_RECIPE = """Preheat the oven to 200 degrees. Combine two cups of flour with a
pinch of salt. Never overmix the batter; all the lumps should remain. First cream the
butter and sugar together until they are light. When the mixture is smooth, fold in the
eggs one at a time. Bake for forty minutes or until golden. Let it cool on a rack before
you slice it. This works the same way for muffins and for a plain sponge."""

ITEM_TEXTS = {
    "movement-navigation": "How an agent plans movement and navigation across a grid.",
    "billing-architecture": "The billing architecture and its framework for invoices.",
    "npc-chat-system-architecture": "Chat system architecture for non-player characters.",
    "utilization-score-semantics": "Two score families with different formulas; "
                                   "loop_state and utility_ratio drift when stale.",
}


def _structural_helpful(item_id, item_text, goal_text):
    """The shipped predicate: id substring, else >=1 STRUCTURAL token overlap."""
    lc = goal_text.lower()
    if item_id.lower() in lc:
        return True
    goal_tokens = {t.strip("-_") for t in retrieve._TOKEN_RE.findall(lc)}
    tokens = retrieve._distinctive_tokens(item_text)
    return any(t in goal_tokens and retrieve._is_structural_token(t)
               for t in tokens)


def test_negative_control_scores_zero():
    """THE load-bearing assertion. Topically-unrelated text must mark NOTHING
    helpful. Before the fix a cake recipe scored 0.627 across real manifests."""
    hits = [k for k, v in ITEM_TEXTS.items()
            if _structural_helpful(k, v, CAKE_RECIPE)]
    assert hits == [], (
        f"cake recipe marked {hits} helpful — the classifier is measuring "
        f"goal-text length again, not relevance (g-115-3134)")


def test_positive_control_still_scores_helpful():
    """A shared IDENTIFIER is real evidence and must survive the tightening —
    otherwise the fix trades false positives for a dead signal."""
    goal = "Investigate why loop_state drifts when utility_ratio goes stale."
    assert _structural_helpful("utilization-score-semantics",
                               ITEM_TEXTS["utilization-score-semantics"], goal)


def test_category_word_alone_is_not_evidence():
    """The exact observed failure: a goal in category framework-architecture
    marked every *-architecture node helpful because its prose contained the
    word "architecture"."""
    goal = ("Fix the framework architecture of the token classifier so the "
            "architecture stops matching on prose.")
    for key in ("billing-architecture", "npc-chat-system-architecture"):
        assert not _structural_helpful(key, ITEM_TEXTS[key], goal), (
            f"{key} marked helpful by the bare word 'architecture'")


def test_id_mention_remains_helpful():
    """Naming the item outright is the strongest signal and must be preserved."""
    goal = "Applied the pattern from movement-navigation to the new solver."
    assert _structural_helpful("movement-navigation",
                               ITEM_TEXTS["movement-navigation"], goal)


# --- schema gate -----------------------------------------------------------

def test_infer_feedback_refuses_pre_v3_sessions():
    """v2 tokens were split on -/_ , so the structural predicate would mark a v2
    session all-noise. Refusing is correct; classifying it would inject bad data
    into times_inferred_helpful (half weight in utility_ratio)."""
    uf = _load("utilization_feedback",
               _ROOT / "core" / "scripts" / "utilization-feedback.py")
    for version in (1, 2):
        assert uf.infer_feedback({"schema_version": version,
                                  "goal_id": "g-000-00"}) is None, (
            f"schema_version={version} must be refused, not classified")


def test_daemon_writes_schema_version_3():
    """The consumer gates on >=3; if the writer regresses to 2 every session is
    silently refused and inferred feedback stops entirely."""
    src = (_ROOT / "mind_api" / "src" / "endpoints" / "retrieve.py").read_text(
        encoding="utf-8", errors="replace")
    assert '"schema_version": 3' in src, (
        "daemon session writer no longer emits schema_version 3 — "
        "infer_feedback refuses everything below 3 (g-115-3144)")


def test_consumer_uses_producer_helpers_not_a_local_copy():
    """Single source of truth: a local regex copy would keep parsing while
    silently matching nothing the day either side changed."""
    src = (_ROOT / "core" / "scripts" / "utilization-feedback.py").read_text(
        encoding="utf-8", errors="replace")
    assert "_prod_token_re()" in src and "_prod_is_structural()" in src
    assert 're.findall(r"[a-z0-9]+", combined_text.lower())' not in src, (
        "consumer re-introduced the old local tokenizer — it splits on -/_ and "
        "makes every structural token unmatchable")
