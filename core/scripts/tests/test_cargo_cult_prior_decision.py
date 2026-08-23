"""PRIOR DECISION preamble on a re-filed contract-floor Idea ( / ).

`already_filed()` deliberately SKIPS terminal goals, so a rebuilt deep streak is
never gated by a stale closed Idea. That skip is correct and must stay — its
title match doubles as the dedup key, and changing the title string would re-file
every existing deduped Idea.

The defect it leaves behind is the MISSING POINTER: the re-filed Idea's
description is generated fresh from the template and names no predecessor, so its
executor starts from a blank page. Measured across four filings of the identical
question (g-335-682, g-335-1034, g-335-1177, g-335-1265) answered with the same
verdict by three different agents — two of whom encoded the answer to a surface
the FILER never reads (a reasoning-bank entry; the recurring goal's own
description).

`prior_decision_block()` supplies the memory WITHOUT touching the gate. These
tests pin both halves: the new preamble, and the untouched dedup semantics.

Run: STORAGE_BACKEND=local py -3 -m pytest \
       core/scripts/tests/test_cargo_cult_prior_decision.py -v
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DETECTOR_PY = REPO / "core" / "scripts" / "cargo-cult-detector.py"

_spec = importlib.util.spec_from_file_location("cargo_cult_detector", DETECTOR_PY)
detector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(detector)

TITLE = "Idea: Rebase original interval for g-999-01"


def _goal(gid, status, note, when=None, title=TITLE):
    g = {"id": gid, "title": title, "status": status, "outcome_note": note}
    if when:
        g["completed_at"] = when
    return g


# ---------------------------------------------------------------- empty cases


def test_no_goals_returns_empty():
    assert detector.prior_decision_block({"goals": []}, TITLE) == ""


def test_no_matching_title_returns_empty():
    asp = {"goals": [_goal("g-1", "completed", "a verdict", "2026-01-01T00:00:00",
                           title="Idea: something else entirely")]}
    assert detector.prior_decision_block(asp, TITLE) == ""


def test_pending_predecessor_returns_empty():
    """A pending same-title goal is already_filed()'s job, not this one.

    If this ever returns a block, the two mechanisms have started overlapping
    and the Idea would cite a predecessor that has not decided anything yet.
    """
    asp = {"goals": [_goal("g-1", "pending", "", None)]}
    assert detector.prior_decision_block(asp, TITLE) == ""


def test_completed_but_empty_outcome_note_returns_empty():
    """Nothing to cite. A block naming a goal with no recorded verdict would
    send the reader to an empty page and cost a lookup for zero information."""
    for note in (None, "", "   ", "\n\t "):
        asp = {"goals": [_goal("g-1", "completed", note, "2026-01-01T00:00:00")]}
        assert detector.prior_decision_block(asp, TITLE) == "", repr(note)


def test_skipped_and_expired_are_not_decisions():
    """already_filed() lumps skipped/expired with completed for GATING. They are
    not verdicts, so they must not be cited as ones."""
    for status in ("skipped", "expired", "blocked", "in-progress"):
        asp = {"goals": [_goal("g-1", status, "some note", "2026-01-01T00:00:00")]}
        assert detector.prior_decision_block(asp, TITLE) == "", status


# ---------------------------------------------------------------- happy path


def test_cites_id_timestamp_and_note():
    asp = {"goals": [_goal("g-335-1177", "completed",
                           "DECISION: neither rebase nor retire. Keep "
                           "interval_hours=10.67 unchanged.",
                           "2026-08-12T23:46:00")]}
    block = detector.prior_decision_block(asp, TITLE)
    assert "PRIOR DECISION" in block
    assert "g-335-1177" in block
    assert "2026-08-12T23:46:00" in block
    assert "neither rebase nor retire" in block
    # The instruction to the reader is the point of the block, not the citation.
    assert "start from it" in block.lower()
    assert block.endswith("\n\n"), "must join cleanly onto the template body"


def test_falls_back_to_completed_date():
    """completed_date is a partial legacy twin of completed_at (measured
    3628/4193). Missing completed_at must not drop the goal from the scan."""
    g = _goal("g-2", "completed", "a verdict")
    g["completed_date"] = "2026-07-04"
    block = detector.prior_decision_block({"goals": [g]}, TITLE)
    assert "g-2" in block and "2026-07-04" in block


def test_missing_both_timestamps_still_cites():
    """A goal with no timestamp is still a recorded decision. Dropping it would
    lose the citation over a cosmetic field."""
    block = detector.prior_decision_block(
        {"goals": [_goal("g-3", "completed", "a verdict")]}, TITLE)
    assert "g-3" in block
    assert "unrecorded time" in block


def test_picks_most_recent_of_several():
    asp = {"goals": [
        _goal("g-old", "completed", "first verdict", "2026-08-03T10:00:00"),
        _goal("g-new", "completed", "latest verdict", "2026-08-16T08:21:00"),
        _goal("g-mid", "completed", "middle verdict", "2026-08-12T23:46:00"),
    ]}
    block = detector.prior_decision_block(asp, TITLE)
    assert "g-new" in block and "latest verdict" in block
    assert "g-old" not in block and "g-mid" not in block


def test_long_note_is_truncated_and_normalised():
    """outcome_notes run to thousands of characters of markdown. Pasting one
    whole would bury the template body it is meant to preface."""
    note = "HEADLINE VERDICT.\n\n## Section\n\n" + ("x" * 5000)
    asp = {"goals": [_goal("g-4", "completed", note, "2026-08-01T00:00:00")]}
    block = detector.prior_decision_block(asp, TITLE)
    assert "HEADLINE VERDICT." in block, "the opening must survive truncation"
    assert "[...]" in block
    assert "\n\n## Section" not in block, "newlines must be normalised away"
    assert len(block) < 1500


def test_excerpt_chars_is_honoured():
    asp = {"goals": [_goal("g-5", "completed", "y" * 900, "2026-08-01T00:00:00")]}
    short = detector.prior_decision_block(asp, TITLE, excerpt_chars=50)
    long_ = detector.prior_decision_block(asp, TITLE, excerpt_chars=800)
    assert len(short) < len(long_)


# ------------------------------------------------- the gate must NOT have moved


def test_already_filed_still_skips_terminal():
    """THE regression guard. already_filed's terminal skip is deliberate: a
    rebuilt streak is a genuinely new escalation. If this flips, every recurring
    contract-floor escalation is silently suppressed by a months-old closed Idea.
    """
    for status in ("completed", "skipped", "expired"):
        asp = {"goals": [_goal("g-1", status, "note", "2026-01-01T00:00:00")]}
        assert detector.already_filed(asp, TITLE) is None, status


def test_already_filed_still_gates_non_terminal():
    for status in ("pending", "in-progress", "blocked"):
        asp = {"goals": [_goal("g-1", status, "", None)]}
        assert detector.already_filed(asp, TITLE) == "g-1", status


def test_dedup_title_string_unchanged():
    """dedup_title doubles as already_filed()'s key. Changing the format string
    re-files every existing deduped Idea — the sharpest edge in this change, and
    the one both source goals flagged in caps."""
    src = DETECTOR_PY.read_text(encoding="utf-8")
    assert 'f"Idea: Rebase original interval for {args.goal_id}"' in src


def test_template_actually_calls_the_helper():
    """A helper nothing calls is indistinguishable from one that never fires.
    Pins the wiring, which no unit test of the helper alone can reach."""
    src = DETECTOR_PY.read_text(encoding="utf-8")
    assert 'f"{prior_decision_block(asp, dedup_title)}"' in src
