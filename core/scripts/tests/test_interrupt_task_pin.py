"""test_interrupt_task_pin.py — pins the directive-hold pin (, sq-019 on ).

The pin shipped with its integration path verified only BY HAND: one six-step
lifecycle run plus two observed live firings. Nothing in the suite failed if the
path broke. The path is:

    loop re-entry -> aspirations/SKILL.md Phase -1.35 -> interrupt-task.sh
    -> interrupt_task.decide() -> the interrupt_task_open WM slot -> hold or proceed

Exactly one link was pinned before this file: test_wm_reset_cadence.py's
parity check, covering only that the slot is in RESET_SURVIVING_SLOTS on both
sides. This file pins the two unpinned links that matter.

TWO GROUPS, DELIBERATELY IN ONE FILE, because they are each other's positive
control (guard-4166 — a mutation proof needs a pin that does NOT flip, and a
control that goes red alongside the fix pins is just a third copy of the same
assertion):

  A. decide() — a pure function, exhaustively covered. It already bit once:
     wm.resolve_slot returns a LOCATOR tuple (parent, key, is_top_level), not a
     value, and reading it as a value produced MALFORMED on a healthy slot AND
     silently disabled the duplicate-pin guard in `open`. That was caught by a
     hand lifecycle run, not by inspection, and nothing would catch a
     reintroduction today.
  B. The Phase -1.35 wiring in aspirations/SKILL.md — prose, deletable or
     rewordable with no signal, INCLUDING its position after Phase -1.4. That
     order is the invariant keeping a real /stop winning over an open pin.

EXPECTED MUTATION OUTCOMES, stated per group BEFORE running (guard-4166 requires
naming which pins go red and which must stay green, then checking both halves):

  Mutant A — delete the Phase -1.35 block from SKILL.md:
      group B RED, group A GREEN  (A is the control)
  Mutant B — invert decide()'s fail-open polarity (non-dict returns "pinned"):
      group A RED, group B GREEN  (B is the control)

Proven with core/scripts/mutation-proof-test.sh, never a hand-rolled
mutate/run/revert: a hand-run leaves the sabotage in place for the whole span
between two edits, on files the live fleet is executing, and a turn that dies in
that window ships it silently (guard-1621, guard-1475).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
SKILL_MD = PROJECT_ROOT / ".claude" / "skills" / "aspirations" / "SKILL.md"

sys.path.insert(0, str(CORE_SCRIPTS))
import interrupt_task  # noqa: E402

decide = interrupt_task.decide


# ───────────────────────── group A: decide() ─────────────────────────

@pytest.mark.parametrize("empty", [None, "", "null"], ids=["None", "empty-str", "null-str"])
def test_empty_slot_yields_none(empty):
    """Nothing pinned is the overwhelmingly common case; it must be quiet."""
    verdict, payload, _ = decide(empty)
    assert verdict == "none"
    assert payload is None


def test_wellformed_dict_is_pinned_and_message_is_the_task_text():
    """The duplicate-pin guard in `open` keys on verdict == 'pinned'; if this
    regresses, that guard silently stops refusing and two user obligations
    overwrite each other."""
    rec = {"task": "reply to the consent-code request", "opened_at": "2026-08-30T10:00:00",
           "source": "user-directive"}
    verdict, payload, message = decide(rec)
    assert verdict == "pinned"
    assert payload == rec
    assert message == "reply to the consent-code request"


def test_wellformed_json_string_is_pinned():
    """The slot round-trips through YAML/JSON, so a string-encoded record is a
    real shape, not a hypothetical one."""
    verdict, payload, message = decide('{"task": "finish the migration", "source": "user"}')
    assert verdict == "pinned"
    assert message == "finish the migration"
    assert payload["source"] == "user"


def test_resolve_slot_locator_tuple_is_malformed_not_pinned():
    """THE REGRESSION THAT ALREADY HAPPENED. wm.resolve_slot returns
    (parent, key, is_top_level). Reading that as a value must be rejected."""
    verdict, payload, message = decide(({"interrupt_task_open": None}, "interrupt_task_open", True))
    assert verdict == "malformed"
    assert payload is None
    assert "tuple" in message


@pytest.mark.parametrize("bad,ident", [
    (["task", "a"],                       "list"),
    ("not json at all",                   "bare-string"),
    (12345,                               "int"),
    ({"opened_at": "2026-08-30"},         "dict-missing-task"),
    ({"task": "   "},                     "dict-blank-task"),
    ({"task": ""},                        "dict-empty-task"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_malformed_shapes_are_malformed(bad, ident):
    verdict, _payload, message = decide(bad)
    assert verdict == "malformed", f"{ident} must be malformed, got {verdict}"
    assert message, "a malformed verdict must carry a LOUD message, never a silent clean"


# The polarity assertion the goal asks for, over every non-pinnable input in one
# place. Fail-open is the deliberate design choice and it is the direction that
# LOSES a task, so an accidental inversion would be invisible in normal
# operation: a wedged loop only shows up when a slot is already broken.
ERROR_INPUTS = [
    ({"interrupt_task_open": None}, "x", True),   # the locator-tuple regression
    ["task", "a"],
    "not json at all",
    12345,
    {"opened_at": "2026-08-30"},
    {"task": "   "},
    None,
    "",
    "null",
]


@pytest.mark.parametrize("value", ERROR_INPUTS, ids=range(len(ERROR_INPUTS)))
def test_fail_open_polarity_no_error_path_yields_pinned(value):
    """FAIL-OPEN POLARITY. No malformed or empty input may ever produce
    'pinned'. `check` maps verdict=='pinned' -> rc 0 (hold) and everything else
    -> rc 1 (proceed), so a 'pinned' here would wedge a healthy loop on a
    broken slot — the failure guard-1562 forbids."""
    verdict, _, _ = decide(value)
    assert verdict != "pinned", (
        f"fail-open polarity INVERTED: {value!r} yielded 'pinned'. Every error "
        f"path must yield none/malformed so the loop proceeds."
    )
    assert verdict in ("none", "malformed")


def test_decide_is_pure_and_does_not_mutate_its_input():
    """Pure by contract — the loop calls it on the live slot value."""
    rec = {"task": "t", "opened_at": "x"}
    before = dict(rec)
    decide(rec)
    assert rec == before


# ───────────────────── group B: Phase -1.35 wiring ─────────────────────

# Anchored on the HEADERS, never a bare "Phase -1.4" substring: that string also
# appears in a cross-reference at the top of the file AND inside the -1.35
# comment itself ("Placed after -1.4"), so a loose match would compare the wrong
# occurrences and the order assertion would silently test nothing
# (guard-2860 — never relax an anchored predicate into a pattern).
HDR_135 = "# Phase -1.35: Directive-Hold Pin"
HDR_14 = "# Phase -1.4: Graceful Stop Handler"
STOP_CHECK = "session-signal-exists.sh stop-requested"
INVOKES = "interrupt-task.sh"


def _skill_text():
    assert SKILL_MD.is_file(), f"aspirations SKILL.md not found at {SKILL_MD}"
    return SKILL_MD.read_text(encoding="utf-8")


def test_phase_135_block_present_and_invokes_the_script():
    text = _skill_text()
    assert HDR_135 in text, (
        "Phase -1.35 (Directive-Hold Pin) is GONE from aspirations/SKILL.md. "
        "A mid-loop user directive no longer survives a turn boundary (g-306-386)."
    )
    tail = text[text.index(HDR_135):text.index(HDR_135) + 1200]
    assert INVOKES in tail, (
        "Phase -1.35 no longer invokes interrupt-task.sh — the phase is prose "
        "with nothing behind it."
    )


def test_phase_135_sits_after_the_phase_14_stop_check():
    """THE ORDER INVARIANT: a real /stop must win over an open pin. If -1.35
    moved above the stop check, a pinned task would hold the loop through a
    user's /stop."""
    text = _skill_text()
    for anchor in (HDR_14, STOP_CHECK, HDR_135):
        assert anchor in text, f"anchor missing from SKILL.md: {anchor!r}"
    assert text.index(HDR_14) < text.index(STOP_CHECK) < text.index(HDR_135), (
        "Phase -1.35 must sit AFTER the Phase -1.4 stop check. Order found: "
        f"-1.4 header @{text.index(HDR_14)}, stop check @{text.index(STOP_CHECK)}, "
        f"-1.35 header @{text.index(HDR_135)}."
    )


def test_control_phase_14_stop_check_still_present():
    """POSITIVE CONTROL (guard-4166). Under the mutant that deletes the -1.35
    block this MUST stay GREEN — it proves the file was still read and parsed,
    so the red above is the pin firing rather than the file being unreadable.
    A control that flips with the fix pins is not a control."""
    text = _skill_text()
    assert HDR_14 in text
    assert STOP_CHECK in text
    assert "Skill(aspirations-graceful-stop)" in text


def test_interrupt_task_shell_wrapper_exists():
    """The phase invokes a wrapper; a phase pointing at a missing script is the
    same defect as a deleted phase, one layer down."""
    assert (CORE_SCRIPTS / "interrupt-task.sh").is_file(), (
        "core/scripts/interrupt-task.sh is missing, but Phase -1.35 invokes it."
    )
