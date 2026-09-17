"""Pins : the `--scan-outcome-note` lane scans deferred_idea ONLY.

WHY THIS EXISTS. A COMPLETED goal's outcome_note is a RETROSPECTIVE — its defect
language describes work the goal DID, not work that remains. The four
present-tense patterns (root_cause, bug_identified, proposed_fix,
unimplemented_action) are written for the `--insight-file` lane, where a finding
is authored DURING execution. Pointed at a retrospective their meaning inverts,
and the more thorough the retrospective the more phantom goals it files.

MEASURED 2026-09-17 (echo, cc-03) over the live completed corpus — 247 goals,
307 note fields, 2,840,431 bytes: 75 signals, of which root_cause 30 +
bug_identified 30 (80%, both HIGH-minting) against deferred_idea 9; only 2 goals
fired deferred_idea alone. Sampling the 60 HIGH-minting signals' minted titles:
26 of 26 were sentence fragments, several self-refuting ("Unblock: Fix THE
SIBLING WORK — it is complete and correctly…"). Three had been filed live.

Each test is written to fail on a specific mutation:
  - drop the lane filter                -> retrospective-suppressed test fails
  - restrict the lane to nothing        -> deferred-idea-survives test fails
  - apply the filter to --insight-file  -> full-lane-unchanged test fails
  - "fix" this by widening the window   -> window-is-not-the-lever test fails
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

GATE_PY = SCRIPTS / "findings-gate.py"

# The real shape, trimmed from 's outcome_note. Two properties matter
# and both are load-bearing: the `.` inside the dotted identifier truncates the
# greedy [^.!?\n] span mid-name, and the sentence that says the work was FIXED
# sits far past the 50-char resolution window.
RETROSPECTIVE = (
    "I READ THE FIRST RUN'S ROWS INSTEAD OF ITS SUMMARY, and that caught a "
    "defect in my own predicate: `_board_paths.channel_paths` was reported as a "
    "literal-signal archive reader because its DOCSTRING explains archive "
    "ordering and contains the string. A census that matches documentation "
    "ABOUT a thing and counts it as a USE of the thing is wrong in the "
    "direction that looks thorough. Fixed by excluding bare-string statements "
    "and anything over 200 chars or containing a newline; the count fell 14 to 11."
)

RECOMMENDATION = (
    "WHAT IS STILL OPEN. Next steps: resolve the ten cross-agent glob readers "
    "against the authoritative bytes, ranked by what a wrong answer costs."
)


@pytest.fixture
def gate():
    spec = importlib.util.spec_from_file_location("findings_gate_under_test", GATE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _types(mod, text, allowed=None):
    return [s["type"] for s in mod.scan_signals(text, allowed_types=allowed)]


def test_mutation_control_retrospective_does_fire_on_the_full_lane(gate):
    """The fixture must be a REAL trigger, or the suppression test below proves
    nothing — an inert string is suppressed by any filter."""
    assert "bug_identified" in _types(gate, RETROSPECTIVE)


def test_retrospective_is_suppressed_on_the_outcome_note_lane(gate):
    assert _types(gate, RETROSPECTIVE, gate.OUTCOME_NOTE_LANE_TYPES) == []


def test_deferred_idea_still_fires_on_the_outcome_note_lane(gate):
    """The lane must not go dark — capturing the forgotten recommendation IS
    the lane's stated purpose."""
    assert "deferred_idea" in _types(gate, RECOMMENDATION, gate.OUTCOME_NOTE_LANE_TYPES)


def test_full_lane_is_unchanged_by_default(gate):
    """allowed_types=None keeps every pattern reachable, so --insight-file
    callers see exactly the pre-change behaviour."""
    assert _types(gate, RETROSPECTIVE, None) == _types(gate, RETROSPECTIVE)
    assert "deferred_idea" in _types(gate, RECOMMENDATION, None)


def test_lane_set_is_exactly_deferred_idea(gate):
    assert gate.OUTCOME_NOTE_LANE_TYPES == frozenset({"deferred_idea"})
    # and it names a pattern that actually exists, so a rename fails loudly
    assert "deferred_idea" in {name for name, _, _ in gate.SIGNAL_PATTERNS}


def test_widening_the_resolution_window_is_not_the_lever(gate):
    """Pins the REJECTED remedy so it is not re-adopted (guard-1719).

    Widening RESOLUTION_SUPPRESSION_CHARS suppresses the FIRST match, and the
    scan then walks to the next already-resolved defect sentence in the same
    retrospective and fires on that instead. Measured on the real note: the
    match moved from "defect in my own predicate: `_board_paths" to
    "DEFECT IN MY FIX, and this is the part worth…".
    """
    two_findings = RETROSPECTIVE + (
        " THE SCOPED SUITE CAUGHT A REAL DEFECT IN MY FIX, and this is the part "
        "worth carrying: a bare module-level flag is wrong for every other "
        "caller. Keying the memo on the root removed the class."
    )
    gate.RESOLUTION_SUPPRESSION_CHARS = 600
    assert "bug_identified" in _types(gate, two_findings), (
        "widening the window was expected to leave a second match firing — if "
        "this now passes cleanly, re-run the corpus measurement before "
        "concluding the window is sufficient"
    )
    # The lane filter, unlike the window, removes it at any window size.
    assert _types(gate, two_findings, gate.OUTCOME_NOTE_LANE_TYPES) == []
