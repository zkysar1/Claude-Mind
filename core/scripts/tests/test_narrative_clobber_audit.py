"""test_narrative_clobber_audit.py —  regression suite.

WHAT THIS PINS. `narrative-clobber-audit.py` asks whether a narrative write
destroyed the note it replaced. Its predicate is CONTAINMENT (does the pre-write
text still appear in the post-write text), which is right and must not be
weakened — a length delta misses the same-size clobber the audit's own docstring
records at +11 chars. But containment is NECESSARY, NOT SUFFICIENT: two
SANCTIONED writes also fail it, and before this goal both were reported as data
loss.

WHY THE OVER-REPORT WAS NOT MERELY NOISE, which is the part worth carrying.
`if clob:` is the ONLY branch that prints the recovery footer, and that footer
sits one line away from `history.py restore` / `history-restore.sh` — destructive
CLI siblings of a safe module function with the identical name, which have caught
three agents (guard-5651, guard-4165; one truncated a live 18MB store by 7.5MB).
So every false positive walked an agent up to the trap. Measured 2026-09-01 on
two boxes independently: cc-07 6 of 6 flagged rows sanctioned, cc-02 4 of 5.
Real data loss on cc-07: ZERO.

THE ASYMMETRY THAT SHAPES EVERY TEST BELOW. This code is an EXEMPTER, not a
detector. A detector that over-matches is noisy and self-announcing; an exempter
that over-matches is a SILENT FALSE MISS that disables the protection (guard-4015).
So the tests that matter most are the ones asserting the exemption does NOT fire:
`test_marker_quoted_in_prose_does_not_exempt` and
`test_recurring_but_hand_written_pre_stays_clobbered`. If those two ever go green
by accident the audit still reports "clean" while blind.

THE TIGHTENING, and why it is deliberately narrower than the filing asked for.
g-115-8514's scope addition proposed skipping rows where `recurring` is true and
the post note carries the closure-evidence marker. That is too broad. The
mechanism's own rule (guard-5679) is that a note WITHOUT the auto stamp was
hand-written and must never be superseded — and closure-evidence-write.sh's
recurring branch has no never-clobber test to enforce it, so it CAN silently
destroy a hand-written note (guard-5049, tracked as g-115-7733). Keying on
`recurring` alone would have hidden that live defect, which is the one TRUE
positive in this class. The exemption therefore requires the auto stamp on the
DESTROYED note.

THE BASH TWIN. `_has_auto_marker` mirrors closure-evidence-write.sh::_ce_marker_ach,
which shipped this same anchoring fix for this same reason (g-115-7853). Two
consumers of one marker convention is exactly the shape guard-4015's corollary
warns about, so `test_marker_constants_match_the_bash_twin` is a drift tripwire:
if either side re-spells the token, this fails rather than one side silently
exempting things the other does not.
"""
import importlib.util
import pathlib
import re
import sys

import pytest

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load():
    """Import the audit by path — its filename has hyphens, so no plain import."""
    spec = importlib.util.spec_from_file_location(
        "narrative_clobber_audit", _SCRIPTS / "narrative-clobber-audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nca = _load()

# A properly-shaped stamp, as closure-evidence-write.sh actually writes it.
AUTO_STAMP = ("[closure-evidence:auto] written 2026-09-01T02:02:48 by "
              "closure-evidence-write.sh (achievedCount=407)")


def _auto_note(body="prior occurrence body"):
    return "%s\n\n%s" % (body, AUTO_STAMP)


# ---------------------------------------------------------------- the marker

def test_properly_shaped_stamp_is_recognised():
    """Positive control. Without this, every negative below is vacuous."""
    assert nca._has_auto_marker(_auto_note()) is True


def test_marker_quoted_in_prose_does_not_exempt():
    """guard-4015: an exemption that matches its own documentation is a silent
    false miss. This is not hypothetical — the closure narrative written for
    g-115-8514 itself quotes the marker while explaining the mechanism, and a
    bare `in` test would have exempted it."""
    note = ("I am explaining the mechanism: closure-evidence-write.sh stamps "
            "notes with [closure-evidence:auto] written <ts> by "
            "closure-evidence-write.sh (achievedCount=N) so the next occurrence "
            "can recognise a machine-written note.")
    assert nca._has_auto_marker(note) is False


def test_token_starting_a_line_without_the_full_shape_does_not_exempt():
    """Mirrors the bash twin's own adversarial fixture: starting a line with the
    token is not enough, the line must carry the written shape."""
    assert nca._has_auto_marker(
        "[closure-evidence:auto] - I am pasting the marker at the start of a line."
    ) is False


def test_empty_and_none_are_not_exempt():
    assert nca._has_auto_marker("") is False
    assert nca._has_auto_marker(None) is False


DEFER_STAMP = ("[closure-evidence:deferred] written 2026-08-30T15:43:37 by "
               "closure-evidence-write.sh (achievedCount=361) - the note above "
               "predates the provenance marker or was hand-written, so THIS "
               "occurrence preserved it rather than superseding it. The NEXT "
               "occurrence (achievedCount > 361) MAY supersede it.")


def test_deferred_stamp_is_a_provenance_stamp_too():
    """FOUND ONLY BY WIDENING THE WINDOW. At the default --examine 20 this token
    is invisible; at 68 the single CLOBBERED row on cc-07 was a deferred->
    superseded transition (g-115-105, 7832 -> 4815). The prior occurrence
    DECLINED to supersede and said in the note itself that the next one may."""
    assert nca._provenance_stamp("body\n\n" + DEFER_STAMP) == "deferred"
    assert nca._provenance_stamp(_auto_note()) == "auto"
    assert nca._provenance_stamp("an ordinary hand-written note") is None


def test_deferred_supersede_names_the_hand_written_exposure():
    """Sanctioned, but it is the ONE place the framework knowingly destroys
    possibly-hand-written text — the row must say so rather than reading like
    any other supersede."""
    v, why = nca._classify("prior body\n\n" + DEFER_STAMP,
                           _auto_note("occurrence N"),
                           {"recurring": True})
    assert v == "superseded"
    assert "hand-written" in why and "g-115-7733" in why


def test_deferred_token_quoted_in_prose_does_not_exempt():
    note = ("Explaining: a note may carry [closure-evidence:deferred] written "
            "<ts> by closure-evidence-write.sh (achievedCount=N) when the "
            "occurrence declines to supersede.")
    assert nca._provenance_stamp(note) is None


def test_deferred_token_without_the_shape_does_not_exempt():
    assert nca._provenance_stamp(
        "[closure-evidence:deferred] pasted at a line start, no shape") is None


def test_marker_constants_match_the_bash_twin():
    """Drift tripwire across the two consumers of one marker convention.

    Asserts the LITERAL agrees with closure-evidence-write.sh. This reads the
    sibling's source deliberately and narrowly — it is a constant-agreement
    check, not a test that builds its population by grepping a name
    (guard-5611). The sibling's own suite does the same in reverse.
    """
    src = (_SCRIPTS / "closure-evidence-write.sh").read_text(encoding="utf-8")
    assert 'CE_AUTO_MARK="%s"' % nca.CE_AUTO_MARK in src, (
        "the audit and closure-evidence-write.sh disagree on the auto marker; "
        "one side will exempt notes the other does not")
    assert 'CE_DEFER_MARK="%s"' % nca.CE_DEFER_MARK in src, (
        "the audit and closure-evidence-write.sh disagree on the deferred marker")
    assert nca.CE_AUTO_SHAPE in src, (
        "the audit's shape anchor no longer appears in the bash matcher")


# -------------------------------------------------------------- the verdicts

def test_containment_still_wins_when_text_is_preserved():
    v, _ = nca._classify("old body", "old body\n\nplus an append", {})
    assert v == "preserved"


def test_absent_pre_is_new_not_loss():
    v, _ = nca._classify("", "a first note", {})
    assert v == "new"


def test_genuine_clobber_is_still_reported():
    """The whole point of the instrument. A hand-written note replaced by
    unrelated text on a one-shot goal is data loss and must stay loud."""
    v, _ = nca._classify("a hand-written finding", "something else entirely",
                         {"recurring": False})
    assert v == "CLOBBERED"


def test_same_size_replacement_is_still_a_clobber():
    """The class the containment predicate exists for: a length check reads +11
    chars and sees nothing wrong."""
    pre = "A" * 200
    post = "B" * 211
    v, _ = nca._classify(pre, post, {"recurring": False})
    assert v == "CLOBBERED"


def test_recurring_auto_to_auto_is_superseded_not_loss():
    v, why = nca._classify(_auto_note("occurrence N-1"),
                           _auto_note("occurrence N"),
                           {"recurring": True})
    assert v == "superseded"
    assert "auto-note" in why


def test_recurring_but_hand_written_pre_stays_clobbered():
    """THE LOAD-BEARING NEGATIVE (guard-5049 / ). closure-evidence-
    write.sh's recurring branch has no never-clobber test, so it CAN destroy a
    hand-written note. That is the one true positive in this class and the
    exemption must not swallow it."""
    v, _ = nca._classify("a hand-written artifact with no provenance stamp",
                         _auto_note("occurrence N"),
                         {"recurring": True})
    assert v == "CLOBBERED"


def test_non_recurring_with_auto_stamp_stays_clobbered():
    """The exemption is a conjunction; neither half alone may license it."""
    v, _ = nca._classify(_auto_note("prior"), "unrelated replacement",
                         {"recurring": False})
    assert v == "CLOBBERED"


def test_missing_goal_record_does_not_exempt():
    """Fail toward the loud side when the record cannot be resolved."""
    v, _ = nca._classify(_auto_note("prior"), "unrelated replacement", None)
    assert v == "CLOBBERED"


# --------------------------------------------------------------- the restore

def test_restore_is_recognised_when_older_text_is_reinserted():
    """A repair puts recovered history back into the MIDDLE of the field, so the
    immediately-prior text stops being contiguous and containment reads the
    remedy as the disease."""
    v, why = nca._classify("remnant tail only", "recovered head\nMIDDLE INSERT\nremnant",
                           {"recurring": False},
                           restore_lookup=lambda: "2026-08-31T22-42-55")
    assert v == "restored"
    assert "2026-08-31T22-42-55" in why


def test_restore_lookup_is_not_consulted_for_preserved_rows():
    """Laziness is load-bearing: each lookback reconstructs the whole store from
    its blob chain, so it must run only for rows that would otherwise report
    loss."""
    calls = []

    def lookup():
        calls.append(1)
        return None

    nca._classify("old body", "old body plus more", {}, restore_lookup=lookup)
    assert calls == [], "restore lookback ran on a row that never failed containment"


def test_restore_lookup_absent_falls_back_to_clobbered():
    v, _ = nca._classify("a hand-written finding", "something else", {}, None)
    assert v == "CLOBBERED"


# ------------------------------------------------------------ the instrument

def test_reclassified_rows_are_not_dropped_from_the_report():
    """An exemption you cannot audit is how a silent false miss survives review.
    The reclassified verdicts must remain printable rows, not deletions."""
    src = (_SCRIPTS / "narrative-clobber-audit.py").read_text(encoding="utf-8")
    # every row appended is printed; assert no filter drops the new verdicts
    assert re.search(r'for r in rows:', src), "the table no longer iterates all rows"
    assert 'superseded=%d restored=%d' in src, (
        "the reclassified counts are no longer reported beside CLOBBERED")


def test_exit_code_and_footer_key_on_clobbered_only():
    """The footer carries the destructive-sibling warning and must fire for real
    loss only — that is the entire point of the reclassification."""
    src = (_SCRIPTS / "narrative-clobber-audit.py").read_text(encoding="utf-8")
    assert 'clob = [r for r in rows if r["verdict"] == "CLOBBERED"]' in src
    assert "return 1 if clob else 0" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
