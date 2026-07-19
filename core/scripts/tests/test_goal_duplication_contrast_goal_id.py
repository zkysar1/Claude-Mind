"""Regression test for : contrast-cited goal-ids are not co-signals.

A goal-id cited in a CONTRAST clause ("distinct from g-318-37", "complements
g-001-320", "unlike g-115-1655") disclaims duplication — but its [-_0-9] shape
otherwise makes it a false "structural co-signal" in the fuzzy keyword-overlap
path, inflating the duplication score (a recurring slice of the 3252
override-ledger entries). `_extract_signals` now drops a goal-id from the
keyword set ONLY when every occurrence sits in such a contrast context; a
goal-id in NEUTRAL / shared-work context is KEPT (it can be genuine duplicate
evidence paired with a topical token — preserving structural-co-signal cases
G3/G9). Genuine goal-id RELATIONSHIPS stay handled by the dedicated
_lineage_relation + Strategy-1 (origin_signal exact-match) paths.

Sibling of the exclusion-context file-path drop (g-115-2207, case G10 in
test_goal_duplication_gate_structural_co_signal.py) — same clause-scoped
look-back helper, different marker set.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.goal_duplication import _extract_signals  # noqa: E402


def _kw(title: str, description: str) -> set:
    """Return the keyword set _extract_signals derives from prose."""
    _, keywords, _ = _extract_signals({"title": title, "description": description})
    return keywords


# --- Contrast context → goal-id DROPPED (the FP being fixed) ------------------

def test_distinct_from_drops_goal_id():
    kw = _kw("Seed the bootstrap vault key on cc-05",
             "Provision the vault key via the self-service provisioner. This is "
             "distinct from g-318-37, which is a manual EFS-bootstrap step.")
    assert "g-318-37" not in kw, kw


def test_complements_drops_goal_id():
    kw = _kw("Producer follow-up",
             "This complements g-001-320 but files a distinct producer change.")
    assert "g-001-320" not in kw, kw


def test_unlike_drops_goal_id():
    kw = _kw("New eviction pass",
             "Unlike g-115-1655 (a completed eviction sweep), this files a new run.")
    assert "g-115-1655" not in kw, kw


# --- Neutral / shared context → goal-id KEPT (preserves G3/G9) ----------------

def test_neutral_context_keeps_goal_id():
    """A goal-id in shared-work (non-contrast) context stays a co-signal —
    the exact assumption structural-co-signal cases G3/G9 rely on."""
    kw = _kw("Fix loop_state corruption",
             "Repair the loop_state regression tracked in g-115-999 across the "
             "recurring close path.")
    assert "g-115-999" in kw, kw


def test_bare_goal_id_reference_kept():
    """No contrast marker at all → the goal-id is retained."""
    kw = _kw("Wire the consumer",
             "Consume the sentinel written by g-115-2416 in the state-update phase.")
    assert "g-115-2416" in kw, kw


# --- Precision: contrast marker must not over-strip topical tokens ------------

def test_contrast_marker_does_not_strip_non_goal_id_tokens():
    """The drop is gated on _GOAL_ID_RE.fullmatch — a topical hyphenated token
    in a contrast clause (e.g. a model name) is NOT a goal-id and stays."""
    kw = _kw("Model eval",
             "Unlike gpt-4 baselines, this evaluates the capability-routing path.")
    # gpt-4 has one digit-group, not the g-NNN-NN goal-id shape → kept.
    assert "gpt-4" in kw, kw
    # A real topical token in the same clause is unaffected.
    assert "capability-routing" in kw, kw


# --- guard-958 adversarial recall controls -----------------------------------
# guard-958: when fixing a keyword-match gate FP, ALWAYS verify RECALL with a
# genuine-positive control where a SINGLE surviving keyword is the SOLE matcher
# — a multi-keyword case (loop_state + goal-id) MASKS single-keyword recall loss.

def test_recall_goal_id_sole_cosignal_neutral_kept():
    """A goal-id that is the SOLE structural co-signal, cited neutrally, must
    be KEPT — proving the contrast disqualifier did not over-suppress a genuine
    single-keyword positive (guard-958 recall control)."""
    kw = _kw("Resolve the tracked item",
             "Resolve the item tracked under g-115-999.")
    assert "g-115-999" in kw, kw


def test_contrast_marker_in_prior_clause_does_not_disqualify():
    """Clause-scoping precision (guard-958 'adjacent to the trigger'): a
    contrast marker in a PRIOR sentence must NOT taint a goal-id cited neutrally
    in its OWN clause — _CLAUSE_DELIM_RE trims the look-back to the goal-id's
    clause, so 'distinct' in sentence 1 cannot disqualify a goal-id in
    sentence 2."""
    kw = _kw("Wire consumer",
             "This is distinct work overall. Consume the sentinel from g-115-2416.")
    assert "g-115-2416" in kw, kw
