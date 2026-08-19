""" — pin the decomposition suffix on every id-extraction site that
feeds a LOOKUP.

WHAT THIS PINS, AND WHY IT IS NARROW. The audit predicate is a CONJUNCTION:
(1) the regex extracts a goal id, AND (2) the extracted value is then used to
look something up and a decision is made on the result. Only sites meeting both
are pinned here. Sites that merely detect, redact, scrub or stamp are
deliberately OUT of scope — widening them uniformly would change redaction
behaviour, which guard-1561 and the goal both warn against. If you add a case
below, first check that its captured id reaches a `by_id.get(...)`-shaped read.

WHY TRUNCATION IS SILENT, and therefore why a test is the only defence: a
truncated id is a WELL-FORMED goal id. It passes GOAL_ID_RE, it reads fine in a
log line, and the lookup SUCCEEDS in the sense that it returns cleanly — it just
returns None, or worse, a different real record. The observable symptom is
"dep g-306-132 not found in queues", which reads as a deleted dependency: a data
problem, not a regex one. Nothing fails loudly at any point (guard-2414).

HOW IT REDDENS. Every case asserts the captured id EQUALS the full suffixed id
in the input. Removing `(?:-[a-z])?` from any pinned pattern makes its capture
`g-306-132` instead of `g-306-132-a`, and the case fails. Note that asserting
only "the capture matches GOAL_ID_RE" would NOT redden — `g-306-132` is itself a
perfectly valid goal id, which is the whole reason this class survives. The
equality assertion is load-bearing; do not relax it to a shape check.

The patterns are read from the SHIPPED modules, never re-declared here
(guard-920): a copy would keep passing after the production regex regressed.
"""
import importlib.util
import re
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(stem: str):
    """Import a core/scripts module whose filename contains hyphens."""
    path = SCRIPTS / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ssot() -> re.Pattern:
    """The single source of truth for goal-id shape: aspirations.py GOAL_ID_RE."""
    return _load("aspirations").GOAL_ID_RE


# (module stem, attribute, sample text, expected FULL capture)
#
# Samples are REAL strings measured on cc-07 2026-08-10, not invented shapes —
# -a and -c are live decomposition children, and the two
# origin_signal samples are verbatim from live goals  and .
CASES = [
    ("defer-recheck", "DEP_STRUCTURED",
     "blocked_on_dependency: g-250-03-c is still pending", "g-250-03-c"),
    ("defer-recheck", "DEP_PROXIMITY",
     "blocked on g-306-132-a landing first", "g-306-132-a"),
    ("defer-recheck", "_GID_RE",
     "gated on g-306-119-a and more", "g-306-119-a"),
    ("routing-audit-target-status-sweep", "ORIGIN_SIGNAL_PATTERN",
     "routing-either-resolve:g-306-132-a", "g-306-132-a"),
    ("routing-audit-target-status-sweep", "TITLE_TARGET_PATTERN",
     "Investigate: routing-either-resolve g-306-132-a intended_agent=either",
     "g-306-132-a"),
    ("unblock-parent-status-sweep", "ORIGIN_SIGNAL_PATTERN",
     "unblock:g-250-03-c", "g-250-03-c"),
    ("unblock-parent-status-sweep", "TITLE_FOR_PATTERN",
     "Unblock: deploy the bridge for g-306-119-a", "g-306-119-a"),
    ("unblock-parent-status-sweep", "GOAL_ID_PATTERN",
     "g-250-03-c", "g-250-03-c"),
    # AGENT_QUALIFIED_STARVED_PATTERN captures (owner, goal-id); group(2) feeds
    # the parent-status lookup. Sample mirrors recurring-starvation-check's
    # agent-source origin_signal form.
    # (TITLE_ID_SCAN was retired 2026-08-13 with the  duplicate-impl
    # resolution — origin/main's implementation won; its unanchored embedded
    # scan GOAL_ID_EMBEDDED_PATTERN is deliberately suffix-exempt per its own
    # comment, so it is NOT enumerated here.)
    ("unblock-parent-status-sweep", "AGENT_QUALIFIED_STARVED_PATTERN",
     "unblock:recurring-starved-alpha-g-306-132-a", "g-306-132-a"),
]


@pytest.mark.parametrize("stem,attr,text,expected", CASES,
                         ids=[f"{c[0]}::{c[1]}" for c in CASES])
def test_lookup_feeding_pattern_captures_decomposition_suffix(stem, attr, text, expected):
    pattern = getattr(_load(stem), attr)
    m = pattern.search(text)
    assert m is not None, (
        f"{stem}.{attr} did not match {text!r} at all. An anchored pattern that "
        f"declines a suffixed id is not benign here: the record falls through to "
        f"a laxer fallback which truncates it instead of skipping it."
    )
    # lastindex, not group(1): multi-group patterns (AGENT_QUALIFIED_STARVED)
    # put the goal id in the LAST matched group; single-group cases are index 1.
    got = m.group(m.lastindex) if m.groups() else m.group(0)
    assert got == expected, (
        f"{stem}.{attr} captured {got!r}, truncating the decomposition suffix off "
        f"{expected!r}. This value feeds a lookup, so the truncation resolves to a "
        f"DIFFERENT record (usually a nonexistent one) and the caller decides on "
        f"that. Restore `(?:-[a-z])?` in the pattern."
    )


@pytest.mark.parametrize("stem,attr,text,expected", CASES,
                         ids=[f"{c[0]}::{c[1]}" for c in CASES])
def test_captured_id_conforms_to_ssot(stem, attr, text, expected):
    """The capture must be a goal id the SSOT recognises.

    Deliberately paired with the equality test above rather than replacing it:
    this one alone passes on a truncated capture, because a truncated id is
    still SSOT-valid. It catches the opposite error — a pattern widened so far
    it swallows trailing text that is not part of any id.
    """
    pattern = getattr(_load(stem), attr)
    m = pattern.search(text)
    assert m is not None
    # lastindex, not group(1): multi-group patterns (AGENT_QUALIFIED_STARVED)
    # put the goal id in the LAST matched group; single-group cases are index 1.
    got = m.group(m.lastindex) if m.groups() else m.group(0)
    assert _ssot().match(got), (
        f"{stem}.{attr} captured {got!r}, which aspirations.GOAL_ID_RE rejects. "
        f"The pattern is now laxer than the SSOT and is capturing non-id text."
    )


def test_ssot_itself_admits_the_suffix():
    """Positive control. If the SSOT ever drops `(-[a-z])?`, every case above
    becomes vacuous — they would be pinning a shape the system no longer has.
    This asserts the premise the whole file rests on, so that change fails HERE
    with a clear reason rather than silently draining the file of meaning."""
    ssot = _ssot()
    assert ssot.match("g-306-132-a"), (
        "aspirations.GOAL_ID_RE no longer admits the decomposition suffix. "
        "Either the SSOT regressed, or decomposition ids genuinely changed shape "
        "— in which case this whole test file needs re-deriving, not deleting."
    )
    assert ssot.match("g-306-132"), "plain ids must still be valid"
    assert not ssot.match("g-306-132-ab"), "suffix is a SINGLE letter"
