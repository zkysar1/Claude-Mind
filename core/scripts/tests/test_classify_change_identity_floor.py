"""Regression pins for classify_change's identity-file cosmetic floor ().

MEASURED IN ZDS PROD (g-001-274, cross-world-injected by omni): agents/<agent>/
self.md stores each numbered Operating Principle as ONE long line, so replacing
a principle's entire content is added=1 + removed=1 = line delta 2 with no
heading change -> 'cosmetic' under the pure line-count proxy. evolution-complete
Phase 5 auto-posts the decisions board and auto-emails the user ONLY for
material agent_self changes, so a permission-reversing identity edit reached
nobody — silently disabling the guard-380 notify-after promise the 2026-04-22
autonomy trade rests on. Second defect: file_kind was accepted in the signature
and never read in the body (the inert-mechanism class).

The fix makes file_kind LOAD-BEARING: for identity kinds (agent_self, program)
the cosmetic floor ALSO requires a character-level bound
(_IDENTITY_COSMETIC_CHAR_BOUND), separating a typo (1-3 chars) from a one-line
principle rewrite (100+ chars) at the SAME line delta. Non-identity kinds keep
the pre-fix line-count floor unchanged.

guard-1988 provenance: the two *_is_material pins below were run against the
UNFIXED function first and FAILED (both returned 'cosmetic'); the failure
output is recorded in g-115-4199's verify evidence. The behavioral pair
(identity material vs script_edit cosmetic on the SAME edit) is also the
proof that file_kind is now read in the body — outcome 4 of the goal.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


EREC = _load("erec_classify_floor", "evolution-record.py")


BEFORE = """---
topic: test-self
---

# Self

## Operating Principles

1. **Evidence over vibes.** do NOT bid before graduation -- the gate exists because ungated bids cost real money and the curriculum is the evidence bar that earns the capability.
2. **Ship working code.** Tests per the repo's CLAUDE.md, then commit and push.
"""

# Permission REVERSAL of principle 1: entire content replaced, ONE line changed
# (line delta = 2: one removed + one added), no heading change. The ZDS repro class.
AFTER_REVERSAL = BEFORE.replace(
    "1. **Evidence over vibes.** do NOT bid before graduation -- the gate exists "
    "because ungated bids cost real money and the curriculum is the evidence bar "
    "that earns the capability.",
    "1. **Evidence over vibes.** bid/no-bid is the manager's call now -- graduation "
    "gating retired; place bids at discretion whenever expected value is positive.",
)

# Pure typo fix: one character on one line (same line delta = 2).
AFTER_TYPO = BEFORE.replace("commit and push.", "commit and push!")

# New H2 heading introduced (structural change).
AFTER_NEW_HEADING = BEFORE + "\n## New Section\n\ncontent\n"

H_BEFORE = "hash-before"
H_AFTER = "hash-after"


def _sanity_fixture_shapes():
    """The fixtures must reproduce the measured defect SHAPE or the pins prove nothing."""
    added, removed, _ = EREC.diff_stats(BEFORE, AFTER_REVERSAL)
    assert added + removed == 2, f"reversal fixture must be line-delta 2, got {added + removed}"
    added, removed, _ = EREC.diff_stats(BEFORE, AFTER_TYPO)
    assert added + removed == 2, f"typo fixture must be line-delta 2, got {added + removed}"


def test_fixture_shapes_match_measured_defect():
    _sanity_fixture_shapes()


def test_identity_one_line_rewrite_is_material():
    # THE regression pin: agent_self one-line principle rewrite at line-delta 2.
    got = EREC.classify_change(BEFORE, AFTER_REVERSAL, H_BEFORE, H_AFTER, "agent_self")
    assert got == "material", f"agent_self one-line principle rewrite must be material, got {got!r}"


def test_program_one_line_rewrite_is_material():
    got = EREC.classify_change(BEFORE, AFTER_REVERSAL, H_BEFORE, H_AFTER, "program")
    assert got == "material", f"program one-line rewrite must be material, got {got!r}"


def test_identity_typo_stays_cosmetic():
    # NEGATIVE control (goal outcome 3): the fix must not make everything material.
    got = EREC.classify_change(BEFORE, AFTER_TYPO, H_BEFORE, H_AFTER, "agent_self")
    assert got == "cosmetic", f"agent_self 1-char typo must stay cosmetic, got {got!r}"


def test_non_identity_kind_keeps_line_floor():
    # Scope pin: the stricter char floor is identity-only; script_edit keeps the
    # pre-fix line-count behavior on the very same edit. Together with
    # test_identity_one_line_rewrite_is_material this is the behavioral proof
    # that file_kind is read in the body (goal outcome 4).
    got = EREC.classify_change(BEFORE, AFTER_REVERSAL, H_BEFORE, H_AFTER, "script_edit")
    assert got == "cosmetic", f"script_edit same edit keeps line-floor cosmetic, got {got!r}"


def test_heading_change_still_material_any_kind():
    for kind in ("agent_self", "program", "script_edit"):
        got = EREC.classify_change(BEFORE, AFTER_NEW_HEADING, H_BEFORE, H_AFTER, kind)
        assert got == "material", f"new-heading change must be material for {kind}, got {got!r}"


def test_bootstrap_and_empty_unchanged():
    assert EREC.classify_change("", BEFORE, None, H_AFTER, "agent_self") == "bootstrap"
    assert EREC.classify_change(BEFORE, BEFORE, H_BEFORE, None, "agent_self") == "empty"
    assert EREC.classify_change(BEFORE, BEFORE, H_BEFORE, H_BEFORE, "agent_self") == "empty"


def test_changed_char_count_is_bounded_to_changed_lines():
    # The helper char-diffs only the changed-line region, so identical bulk
    # text contributes zero regardless of file size.
    big = ("x" * 80 + "\n") * 500
    a = big + "tail line one\n"
    b = big + "tail line two\n"
    n = EREC.changed_char_count(a, b)
    assert 0 < n <= len("tail line one") + len("tail line two"), n


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
