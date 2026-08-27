"""Regression test for  candidate (b): a goal's OWN declared
provenance goal-id is not a keyword co-signal.

Citing the goal that discovered the work is standard practice, and that
discovering goal's own closure record names the same id — so the citation
MANUFACTURES the keyword overlap it is then blocked on. The filing goal's
REFUSAL 2 was exactly this: a block against a goal closed three hours earlier
where `file_path_hits` was EMPTY and the surviving keyword hits were stopwords
(after, author, closed, identifier) plus the discovering goal-id itself.

`_extract_signals` now drops from the keyword set any goal-id the goal DECLARES
as its own provenance — `discovered_by`, or one cited inside `origin_signal`.

WHY A DECLARED FIELD AND NOT A PROSE MARKER, which is the whole design and the
reason this is a sibling of the contrast test rather than an extension of it:
the neutral-context cases pinned in test_goal_duplication_contrast_goal_id.py
read "the sentinel written by g-115-2416" and "the item tracked under
g-115-999". That is provenance-SOUNDING phrasing which is deliberately pinned as
GENUINE co-signal (preserving structural-co-signal cases G3/G9). A prose marker
set would drop exactly those, repeating the candidate-(c) regression this goal
already recorded — a port that broke pinned true positive G11. A declared field
is unambiguous and is invisible to every prose-only case, which is why every
pre-existing test in this suite is untouched by the change.

No lineage signal is lost: genuine goal-id RELATIONSHIPS are handled by the
dedicated _lineage_relation + Strategy-1 (origin_signal exact-match) paths. This
drop narrows only the FUZZY KEYWORD-OVERLAP path.

guard-958 / the keyword-classifier control rule: a fix whose effect is that
something STOPS APPEARING needs a positive control that does NOT flip. Every
drop assertion below is paired with a keep assertion IN THE SAME CALL, so an
over-broad implementation that stripped all goal-ids would fail here rather than
passing silently.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.goal_duplication import _extract_signals  # noqa: E402


def _kw(title: str, description: str, **fields) -> set:
    """Keyword set _extract_signals derives, with optional goal fields set."""
    goal = {"title": title, "description": description}
    goal.update(fields)
    _, keywords, _ = _extract_signals(goal)
    return keywords


# --- declared provenance -> DROPPED, with a same-call positive control --------

def test_discovered_by_id_dropped_while_unrelated_id_kept():
    """The `discovered_by` id is dropped; a DIFFERENT neutrally-cited id in the
    same description is KEPT. Both assertions run on ONE call, so a blanket
    goal-id strip fails the second."""
    kw = _kw("Fix the recurring-close sentinel",
             "Discovered while closing g-115-9001. Repairs the sentinel "
             "written by g-115-2416 in the state-update phase.",
             discovered_by="g-115-9001")
    assert "g-115-9001" not in kw, kw          # provenance -> dropped
    assert "g-115-2416" in kw, kw              # positive control -> KEPT


def test_origin_signal_id_dropped_while_unrelated_id_kept():
    """A goal-id inside `origin_signal` (e.g. a decomposition parent) is
    provenance. Same-call positive control as above.

    THE PARENT ID MUST APPEAR IN THE DESCRIPTION TOO, and that is not padding.
    `_extract_signals` derives keywords from title+description only, so an id
    living solely in `origin_signal` is never a keyword candidate and asserting
    its absence would pass with the fix REVERTED — a vacuous assertion. Caught
    by the mutation proof: this test alone did not flip when the drop was
    commented out. The prose citation is what makes it a real candidate.
    """
    kw = _kw("Build half of the control protocol",
             "Decomposed from g-368-05. Implements the spec from g-368-26 and "
             "is tracked in g-115-999.",
             origin_signal="decomposition:g-368-05")
    assert "g-368-05" not in kw, kw            # provenance -> dropped
    assert "g-115-999" in kw, kw               # positive control -> KEPT
    assert "g-368-26" in kw, kw                # neutral citation -> KEPT


# --- the drop is keyed on the FIELD, not on prose ----------------------------

def test_same_prose_without_the_field_keeps_the_id():
    """THE DISCRIMINATOR. Byte-identical prose, field absent -> the id is KEPT.
    This is what proves the drop reads the declared field rather than the
    surrounding words, and it is why no pre-existing test changes behaviour."""
    prose = "Discovered while closing g-115-9001. Repairs the sentinel."
    with_field = _kw("Fix the sentinel", prose, discovered_by="g-115-9001")
    without = _kw("Fix the sentinel", prose)
    assert "g-115-9001" not in with_field, with_field
    assert "g-115-9001" in without, without


def test_non_goal_id_origin_signal_strips_nothing():
    """`origin_signal` usually carries no goal-id at all (idea:, investigate:).
    Such a signal must not remove any token."""
    kw = _kw("Park record has no undo",
             "The parked PRs stay parked and g-115-999 tracks the follow-up.",
             origin_signal="idea:deploy-hold-park-has-no-undo")
    assert "g-115-999" in kw, kw


def test_malformed_discovered_by_is_ignored():
    """`discovered_by` is not always a goal-id (agent names, slugs, None). A
    non-goal-id value must be ignored rather than stripping a lookalike token."""
    for bad in (None, "", "alpha", "asp-115", "g-115", "not a goal id"):
        kw = _kw("Wire the consumer",
                 "Consume the sentinel from g-115-2416.",
                 discovered_by=bad)
        assert "g-115-2416" in kw, (bad, kw)


# --- recall control: provenance drop must not empty a genuine match ----------

def test_topical_tokens_survive_the_provenance_drop():
    """Only the provenance id is removed — the goal's actual subject tokens are
    untouched, so a genuine duplicate still matches on aboutness."""
    kw = _kw("Repair loop_state corruption",
             "Discovered under g-115-9001. Repairs loop_state corruption in the "
             "recurring close path.",
             discovered_by="g-115-9001")
    assert "g-115-9001" not in kw, kw
    assert "loop_state" in kw, kw
    assert "corruption" in kw, kw
    # Deliberately NOT asserting on "recurring"/"close"/"path": the first two
    # are in _STOPWORDS and the third is below the 5-char floor of the
    # extraction regex, so all three are absent with or WITHOUT this change.
    # Asserting on them would pin the stopword list, not the provenance drop —
    # measured when the first draft of this test failed on exactly that.
