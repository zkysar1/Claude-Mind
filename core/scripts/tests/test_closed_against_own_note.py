"""Pins the classifier behind closed-against-own-note-check.py ().

The two controls are REAL note shapes measured 2026-08-29, not invented fixtures:
the incident's own opening line, and the refuting-quote shape that would have been a
false positive. A regression on either silently changes what the detector reports.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from closed_against_own_note import (  # noqa: E402
    TERMINAL_STATUSES, classify_goal, confidence, scan_note,
)

INCIDENT = ("REOPENED BY ITS OWN CRITERIA - do not re-close on a diagnosis. "
            "iteration-close marked this completed and all three criteria are unmet.")
# , 2026-08-29: QUOTES a not-done phrase in order to REFUTE it.
REFUTING_QUOTE = ("SUPERSEDES the prior note, which ended 'NOT YET APPLIED'. That is no "
                  "longer true and would send the next actor to redo committed work.")
#  / : explains the STATUS CHOICE, not a not-done claim.
STATUS_EXPLANATION = ("NO WORK EXISTED — closing as skipped, not completed. This carrier "
                      "is a FALSE POSITIVE of the residual-work gate.")


def test_incident_shape_scores_high():
    assert confidence(scan_note(INCIDENT)) == "high"


def test_refuting_quote_never_rises_above_low():
    hits = scan_note(REFUTING_QUOTE)
    assert hits, "the phrase should still be FOUND — it is the confidence that must be low"
    assert all(h["in_quotes"] for h in hits)
    assert confidence(hits) == "low"


def test_status_explanation_is_not_a_not_done_claim():
    # "skipped, not completed" must not fire the not-done marker at all.
    assert not [h for h in scan_note(STATUS_EXPLANATION) if h["label"] == "not-done"]


def test_non_terminal_goal_is_never_classified():
    for status in ("pending", "in-progress", "blocked"):
        assert classify_goal({"id": "g-x", "status": status, "outcome_note": INCIDENT}) is None


def test_terminal_goal_with_incident_note_is_flagged():
    out = classify_goal({"id": "g-326-736", "status": "completed", "outcome_note": INCIDENT})
    assert out is not None
    assert out["confidence"] == "high"
    assert out["goal_id"] == "g-326-736"


def test_empty_and_missing_notes_are_silent():
    assert scan_note(None) == []
    assert scan_note("") == []
    assert classify_goal({"id": "g-x", "status": "completed"}) is None


def test_terminal_statuses_exclude_open_states():
    assert "pending" not in TERMINAL_STATUSES
    assert "in-progress" not in TERMINAL_STATUSES
    assert "completed" in TERMINAL_STATUSES


def test_progress_note_is_scanned_too():
    # note-vs-STATUS is the point; the assertion can live in either note field.
    out = classify_goal({"id": "g-x", "status": "completed", "progress_note": INCIDENT})
    assert out is not None and "progress_note" in out["fields"]


# --- regressions from the fresh-eyes review of this file (2026-08-29) ---

# FIX 1 (severity: invalidates). Ordinary apostrophes paired up and marked the prose
# between them as quoted, downgrading a real finding to `low` — below the default
# report filter. This is the incident's own language, so the detector was blind to
# exactly what it was built for.
APOSTROPHE_PROSE = ("This goal's three completion criteria are unmet; "
                    "the executor didn't finish the sweep.")


def test_apostrophes_are_not_quote_delimiters():
    hits = scan_note(APOSTROPHE_PROSE)
    assert hits, "the criteria-unmet marker must still be found"
    assert not any(h["in_quotes"] for h in hits), "apostrophes must not form a quote span"
    assert confidence(hits) == "high"


def test_real_single_quotes_still_count_as_quotes():
    # The refuting-quote control must not regress while fixing apostrophes.
    hits = scan_note(REFUTING_QUOTE)
    assert all(h["in_quotes"] for h in hits)


# FIX 2 (severity: constrains). The first suppression used a fixed-width lookbehind
# matching only the literal "skipped, " — a second space, or any other terminal
# status, slipped through and produced a false positive.
def test_status_explanation_suppressed_for_every_terminal_status_and_spacing():
    for text in ("closing as skipped, not completed",
                 "STATUS: skipped,  not completed",
                 "superseded, not completed",
                 "expired,not completed",
                 "BLOCKED , not completed"):
        assert not [h for h in scan_note(text) if h["label"] == "not-done"], text


def test_genuine_not_done_still_fires():
    assert [h for h in scan_note("this is simply not done") if h["label"] == "not-done"]
