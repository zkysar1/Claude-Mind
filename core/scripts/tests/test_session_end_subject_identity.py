"""The session-end report must be one topic PER AGENT, not one topic per fleet.

THE DEFECT (measured 2026-09-10, from world/notifications-sent.jsonl, not from
reading code). Two agents ended two sessions on two boxes 16 minutes apart:

    2026-09-09T20:42:02  alpha    "Session ended - 62 goals closed"  rc=0 SENT
    2026-09-09T20:58:15  foxtrot  "Session ended - 45 goals closed"  rc=4 REFUSED
                                   suppressed_duplicate_of: the alpha row
    2026-09-09T20:59:21  foxtrot  "Session ended - 45 goals closed"  rc=0 SENT
                                   via --allow-duplicate, with a written reason

The refusal was the gate working exactly as written, and foxtrot's override was
correct. But the ritual should never have been needed: the two subjects tokenise
IDENTICALLY. `tokens()` drops tokens of length <= 2, and the goal COUNT was the
only thing that differed between them, so both reduce to
{closed, ended, goals, session} -- jaccard 1.00 against a 0.60 threshold.
`completion` has no WINDOW_HOURS entry, so it inherits `_default` = 168 hours.

Consequence: the FIRST agent in the fleet to stop owned the topic "session
ended" for SEVEN DAYS, and every other agent's shutdown report reached the owner
only by overriding the gate. That is not a dedup, it is a mute.

WHAT THIS FILE PINS, and why it is two assertions and not one. The gate is not
broken -- fuzzy topic matching is deliberate and correct for its purpose ("two
agents asking the same QUESTION should dedupe"). The bug was in the SUBJECT the
skill tells the agent to write, so a test that only exercised
notification_outreach would have passed against the broken system (rb-5828:
callee coverage is not caller evidence). So:

  * test_old_shape_collides       -- the negative control. If this ever stops
                                     failing to distinguish, the gate's matching
                                     changed and the rest of this file is
                                     measuring nothing.
  * test_new_shape_does_not_collide -- the property the fix buys.
  * test_unrelated_subject_does_not_match -- discrimination control, so
                                     test_new_shape is not just "jaccard returns
                                     low numbers for everything".
  * test_skill_specifies_identified_subject -- the CALLER binding. This is the
                                     one that actually fails if someone reverts
                                     the template in SKILL.md.
  * test_bracket_prefix_is_not_a_fix -- pins the trap: putting the agent in a
                                     "[Alpha]" prefix looks equivalent and is
                                     not, because strip_agent_prefix() removes
                                     it before matching.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import notification_outreach as N  # noqa: E402

PROJECT_ROOT = SCRIPTS.parent.parent
CONSOLIDATE = PROJECT_ROOT / ".claude" / "skills" / "aspirations-consolidate" / "SKILL.md"

# The exact strings the ledger recorded.
OLD_ALPHA = "Session ended — 62 goals closed"
OLD_FOXTROT = "Session ended — 45 goals closed"

# The shape Step 9.7 now specifies.
NEW_ALPHA = "alpha session 131 ended — 62 goals closed"
NEW_FOXTROT = "foxtrot session 132 ended — 45 goals closed"


def overlap(a: str, b: str) -> float:
    return N.jaccard(N.tokens(N.normalize_subject(a)), N.tokens(N.normalize_subject(b)))


def matches(a: str, b: str) -> bool:
    return overlap(a, b) >= N.SUBJECT_JACCARD


def test_old_shape_collides():
    """NEGATIVE CONTROL — the defect, reproduced.

    Without this the file cannot tell "the fix works" from "the gate never
    matched anything anyway". The count is what differed in production and it is
    exactly what the tokeniser discards.
    """
    assert "62" not in N.tokens(N.normalize_subject(OLD_ALPHA))
    assert "45" not in N.tokens(N.normalize_subject(OLD_FOXTROT))
    assert overlap(OLD_ALPHA, OLD_FOXTROT) == 1.0
    assert matches(OLD_ALPHA, OLD_FOXTROT), (
        "the historical collision no longer reproduces — the gate's matching has "
        "changed, so re-derive this whole file before trusting it"
    )


def test_new_shape_does_not_collide():
    """Two agents shutting down are two topics."""
    assert not matches(NEW_ALPHA, NEW_FOXTROT), (
        f"overlap {overlap(NEW_ALPHA, NEW_FOXTROT):.2f} still >= "
        f"{N.SUBJECT_JACCARD}: one agent's shutdown would mute every other's"
    )


def test_same_agent_same_session_still_dedupes():
    """The fix must not disable the gate it is threading through.

    A genuine re-send of the SAME agent's SAME session must still be caught —
    otherwise this change trades a mute for a flood.
    """
    again = "alpha session 131 ended — 62 goals closed"
    assert matches(NEW_ALPHA, again)


def test_unrelated_subject_does_not_match():
    """Discrimination control."""
    assert overlap(NEW_ALPHA, "AWS spend 2026-09-09: $26.13") < N.SUBJECT_JACCARD


def test_bracket_prefix_is_not_a_fix():
    """The trap: '[Alpha] Session ended' reads as identified and is not.

    strip_agent_prefix() deletes bracket tags before matching — deliberately, so
    that two agents asking one shared question dedupe. Right for a question,
    wrong for a shutdown report, where the agent IS the topic.
    """
    tagged_a = "[Alpha] Session ended — 62 goals closed"
    tagged_f = "[Foxtrot] Session ended — 45 goals closed"
    assert matches(tagged_a, tagged_f), (
        "if this stops holding, strip_agent_prefix changed and Step 9.7's "
        "'never a bracket prefix' warning needs re-deriving"
    )


def test_skill_specifies_identified_subject():
    """CALLER BINDING — the assertion that fails if the template is reverted.

    Everything above measures notification_outreach, which was never broken. The
    defect lived in the subject aspirations-consolidate Step 9.7 tells the agent
    to write, so that line is what has to be pinned.
    """
    src = CONSOLIDATE.read_text(encoding="utf-8")
    subject_lines = [
        ln for ln in src.splitlines()
        if re.search(r"^\s*-\s*subject:", ln) and "ended" in ln
    ]
    assert subject_lines, "Step 9.7 no longer declares a session-end subject line"
    line = subject_lines[0]
    assert "<agent>" in line, f"session-end subject does not name the agent: {line.strip()}"
    assert "session" in line.lower(), f"session-end subject does not name the session: {line.strip()}"
    assert not re.search(r"subject:\s*\"\[", line), (
        "the agent must be in the subject TEXT, not a bracket prefix — "
        "strip_agent_prefix() would delete it"
    )
