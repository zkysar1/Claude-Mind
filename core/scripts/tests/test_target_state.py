"""test_target_state.py -  Maintain-CHECK-ABOUT extraction regression test.

Pins the contract from g-115-890 (delta 2026-05-17):

  When _target_state.extract_targets() sees a goal whose title matches
  the Maintain-CHECK-ABOUT shape (Maintain: add/wire/ensure [<word>]
  check ...), it REPLACES target_files with [edit_target] (the
  assertion-HOST SKILL.md path) instead of the naturally-extracted
  assertion-TARGET files. This lets probe_target_state check whether
  the new check has landed in the SKILL.md, not whether the assertion's
  TARGET identifiers tautologically appear in source files.

Canonical incident (g-115-875): the goal-duplication-gate probe scanned
orphan-root-sweep.sh + _orphan_root_helpers.py for `is_mode_d_cruft`
and got hit_ratio=1.0 → verdict=already_present → blocked a legitimate
Maintain goal. The fix wires _is_maintain_check_about_goal +
_extract_edit_target so the probe instead checks
.claude/skills/verify-learning/SKILL.md (where the new check needs to
land).

Tests cover both the predicate (regex matching) and the
edit-target priority chain:
  (a) g-115-875 canonical replay -> verify-learning SKILL.md
  (b) generic verify-learning trigger -> verify-learning SKILL.md
  (c) explicit "add check to <skill-name>" -> that skill's SKILL.md
  (d) explicit .claude/skills/<name>/SKILL.md path -> that path
  (e) Maintain without "check" -> unchanged behavior (predicate false)
  (f) "Apply: add check" (not Maintain) -> unchanged behavior (predicate false)

Cross-refs:
  - g-115-890 (this fix's Apply goal)
  - g-115-877 (parent Idea + design rationale)
  - g-115-875 (canonical false-positive incident)
  - _target_state.py:_is_maintain_check_about_goal + _extract_edit_target
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Same lazy import pattern as test_target_state_check_positional.py.
TS_PATH = CORE_SCRIPTS / "_target_state.py"
spec = importlib.util.spec_from_file_location("_target_state", TS_PATH)
ts_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts_mod)

extract_targets = ts_mod.extract_targets
_is_maintain_check_about_goal = ts_mod._is_maintain_check_about_goal
_extract_edit_target = ts_mod._extract_edit_target


# Canonical  title — the false-positive incident that motivated
# . Preserved verbatim so any future regex tightening still has
# to satisfy this concrete case.
G_115_875_TITLE = (
    "Maintain: add verify-learning check for orphan-root-sweep.sh "
    "Scan 4 <-> _orphan_root_helpers.is_mode_d_cruft wiring"
)
G_115_875_DESC = (
    "g-115-756 added Scan 4 to orphan-root-sweep.sh which delegates Mode D "
    "detection to the Python helper _orphan_root_helpers.is_mode_d_cruft "
    "via NUL-delimited stdin pipe. If a future edit (a) renames "
    "is_mode_d_cruft, (b) drops the _orphan_root_helpers.py file, (c) "
    "changes the bash NUL pipe encoding, or (d) breaks the SCRIPT_DIR path "
    "computation, Scan 4 silently no-ops. Suggested verify-learning check: "
    "grep orphan-root-sweep.sh for `from _orphan_root_helpers import "
    "is_mode_d_cruft` AND grep _orphan_root_helpers.py for `def "
    "is_mode_d_cruft`; both must be present."
)


# ─── TestMaintainCheckAbout: 6-case contract from  spec ────────


class TestMaintainCheckAbout:
    """The 6-case contract pinning extract_targets behavior on
    Maintain-CHECK-ABOUT goal shapes vs everything else.
    """

    def test_a_g115875_canonical_replay(self):
        """ canonical incident: predicate fires, target_files
        REPLACED with [.claude/skills/verify-learning/SKILL.md], NOT the
        assertion-target files (orphan-root-sweep.sh, _orphan_root_helpers.py).
        target_kind == 'maintain-check-about'.
        """
        assert _is_maintain_check_about_goal(G_115_875_TITLE) is True
        result = extract_targets(G_115_875_TITLE, G_115_875_DESC)
        assert result["target_files"] == [
            ".claude/skills/verify-learning/SKILL.md"
        ], (
            f"expected target_files replaced with verify-learning SKILL.md; "
            f"got {result['target_files']}"
        )
        assert result["target_kind"] == "maintain-check-about"
        # Sanity: the assertion-target file basenames must NOT appear in
        # target_files anymore (they would, without the replacement).
        joined = " ".join(result["target_files"])
        assert "orphan-root-sweep.sh" not in joined
        assert "_orphan_root_helpers.py" not in joined

    def test_b_generic_verify_learning_trigger(self):
        """Phrase-based trigger: 'add verify-learning check asserting X'
        with no explicit SKILL.md path and no other source files —
        edit_target resolves to verify-learning SKILL.md via priority
        rule (b)."""
        title = "Maintain: add verify-learning check asserting daemon health"
        desc = "Ensure daemon-health.sh exits 0 when daemon responds."
        assert _is_maintain_check_about_goal(title) is True
        result = extract_targets(title, desc)
        assert result["target_files"] == [
            ".claude/skills/verify-learning/SKILL.md"
        ]
        assert result["target_kind"] == "maintain-check-about"

    def test_c_add_check_to_specific_skill(self):
        """'Maintain: add check to /respond' resolves to .claude/skills/respond/SKILL.md
        via priority rule (c). Leading slash on /respond is stripped."""
        title = "Maintain: add check to /respond about persona freshness"
        desc = "After /respond runs, persona should be re-primed."
        assert _is_maintain_check_about_goal(title) is True
        result = extract_targets(title, desc)
        assert result["target_files"] == [
            ".claude/skills/respond/SKILL.md"
        ]
        assert result["target_kind"] == "maintain-check-about"

    def test_d_explicit_skill_md_path(self):
        """Explicit .claude/skills/<name>/SKILL.md path in title wins via
        priority rule (a) — author was specific, trust the author."""
        title = (
            "Maintain: add check in .claude/skills/aspirations-execute/SKILL.md "
            "asserting Phase 4 obligations fire"
        )
        desc = "After Phase 4 runs, all obligations must complete."
        assert _is_maintain_check_about_goal(title) is True
        result = extract_targets(title, desc)
        assert result["target_files"] == [
            ".claude/skills/aspirations-execute/SKILL.md"
        ]
        assert result["target_kind"] == "maintain-check-about"

    def test_e_maintain_without_check_unchanged(self):
        """'Maintain: refactor X' — no 'check' word — predicate is False,
        extract_targets behaves as before (target_kind == None, target_files
        unchanged from natural extraction)."""
        title = "Maintain: refactor _target_state.py to extract helpers"
        desc = "Move _clean_identifier into a separate module."
        assert _is_maintain_check_about_goal(title) is False
        result = extract_targets(title, desc)
        # target_kind must be None — predicate didn't fire.
        assert result["target_kind"] is None
        # Naturally extracted target_files should still include the source
        # file mentioned in the description (no replacement happened).
        assert "_target_state.py" in " ".join(result["target_files"]) or \
            result["target_files"] == []

    def test_f_apply_not_maintain_unchanged(self):
        """'Apply: add check to Y' — Apply prefix, not Maintain — predicate
        is False even though the rest of the title matches the inner pattern.
        target_kind == None, behavior unchanged."""
        title = "Apply: add check to /respond about persona freshness"
        desc = "Implement the persona-freshness check in /respond."
        assert _is_maintain_check_about_goal(title) is False
        result = extract_targets(title, desc)
        assert result["target_kind"] is None
        # The Apply: prefix means the predicate doesn't fire and target_files
        # are NOT replaced. So .claude/skills/respond/SKILL.md should NOT
        # appear in target_files (since /respond is not a literal file path
        # the natural extractor would pick up).
        for fp in result["target_files"]:
            assert fp != ".claude/skills/respond/SKILL.md", (
                f"target_files should not be replaced for Apply: goals; "
                f"got {result['target_files']}"
            )


# ─── Edit-target priority-chain unit tests ──────────────────────────────


class TestExtractEditTargetPriority:
    """Pin the (a)>(b)>(c)>(d) priority of _extract_edit_target so future
    rule additions don't accidentally invert precedence.
    """

    def test_a_beats_b_explicit_path_over_verify_learning_phrase(self):
        """Explicit .claude/skills/X/SKILL.md in text wins over a
        verify-learning trigger phrase that would otherwise route to
        verify-learning SKILL.md."""
        title = "Maintain: add verify-learning check"
        desc = "See .claude/skills/aspirations-execute/SKILL.md for the host."
        result = _extract_edit_target(title, desc)
        assert result == ".claude/skills/aspirations-execute/SKILL.md"

    def test_b_beats_c_verify_learning_over_add_check_to(self):
        """Verify-learning phrase wins over an 'add check to <skill>'
        phrase, because the verify-learning lane is the convention for
        cross-cutting assertions (rb-917 / guard-343)."""
        title = "Maintain: add verify-learning check"
        desc = "Wire the check; consider also exposing in add check to /respond chain."
        result = _extract_edit_target(title, desc)
        assert result == ".claude/skills/verify-learning/SKILL.md"

    def test_c_add_check_to_specific(self):
        """When neither (a) nor (b) fires, 'add check to <skill>'
        resolves via rule (c)."""
        title = "Maintain: add check to aspirations-spark"
        desc = "Ensure spark runs after deep close."
        result = _extract_edit_target(title, desc)
        assert result == ".claude/skills/aspirations-spark/SKILL.md"

    def test_d_default_to_verify_learning(self):
        """When no specific signal exists, default (d) routes through
        verify-learning SKILL.md."""
        title = "Maintain: add check"
        desc = "Generic Maintain-CHECK-ABOUT with no targeting hint."
        result = _extract_edit_target(title, desc)
        assert result == ".claude/skills/verify-learning/SKILL.md"


# ─── Predicate edge-case tests ──────────────────────────────────────────


class TestIsMaintainCheckAboutGoal:
    """Edge cases of the regex predicate that the 6-case suite doesn't
    cover. Pins the exact match boundary so future tightening doesn't
    drop the canonical g-115-875 shape."""

    def test_empty_title(self):
        assert _is_maintain_check_about_goal("") is False
        assert _is_maintain_check_about_goal(None) is False

    def test_wire_verb_accepted(self):
        """The verb alternation accepts 'wire' too (not just 'add')."""
        assert _is_maintain_check_about_goal(
            "Maintain: wire check into pipeline"
        ) is True

    def test_ensure_verb_accepted(self):
        """The verb alternation accepts 'ensure' too."""
        assert _is_maintain_check_about_goal(
            "Maintain: ensure check fires on deep close"
        ) is True

    def test_case_insensitive(self):
        """Regex is case-insensitive — 'MAINTAIN', 'maintain', 'Maintain' all match."""
        assert _is_maintain_check_about_goal("MAINTAIN: ADD CHECK") is True
        assert _is_maintain_check_about_goal("maintain: add check") is True

    def test_hyphenated_optional_word(self):
        """Hyphenated tokens like 'verify-learning' match the optional
        [\\w-]+ word between verb and 'check'. Without this, g-115-875
        would not match."""
        assert _is_maintain_check_about_goal(
            "Maintain: add verify-learning check"
        ) is True
        assert _is_maintain_check_about_goal(
            "Maintain: wire post-state-update check"
        ) is True

    def test_no_check_word(self):
        """Without 'check' in the title, predicate is False even with verb."""
        assert _is_maintain_check_about_goal(
            "Maintain: add new feature"
        ) is False

    def test_check_without_verb(self):
        """Without add|wire|ensure, predicate is False even with 'check'."""
        assert _is_maintain_check_about_goal(
            "Maintain: refactor check semantics"
        ) is False


if __name__ == "__main__":
    import pytest
    # SystemExit propagates pytest's exit code — a bare pytest.main() call
    # discards it, so script-mode invocation (run-invisible-suites.sh) exits 0
    # even with failing tests (2; mirrors
    # test_recurring_close_outcome_origin's shape).
    raise SystemExit(pytest.main([__file__, "-v"]))
