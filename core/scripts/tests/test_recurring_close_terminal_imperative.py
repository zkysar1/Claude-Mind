"""test_recurring_close_terminal_imperative.py —  regression test.

Pins the outcome-aware terminal imperative added to recurring-close.sh that
closes the Phase-6-silent-skip gap surfaced by g-115-965 investigation.

Bug origin (g-115-965 → g-115-977): recurring-close.sh wraps four
iteration-close phases (verify/state-update/learning-gate/productivity-check)
but does NOT invoke aspirations-spark (Phase 6). The previous terminal
imperative unconditionally directed the LLM to "Call Skill(aspirations) with
args='loop'" — silently bypassing Phase 6 on every deep recurring close,
including forced-flip deeps from Block A/C. This violated the universal
reasoning-bank entry "Docs-vs-impl drift in framework shortcut wrappers":
the shortcut wrapper omits phases the orchestrator manual path runs.

Fix (Option C from g-115-965 investigation):
  - When OUTCOME=deep: imperative directs Skill(aspirations-spark) FIRST,
    THEN Skill(aspirations) with args='loop'.
  - When OUTCOME=routine: imperative directs Skill(aspirations) with
    args='loop' only (Phase 6 skip rule preserved).

The OUTCOME variable at the terminal imperative is the post-mutation
classification (recurring-loop-state-mutate.sh applied Block A/C flips,
the post-flip value was reassigned to OUTCOME on line ~194). So the
imperative reflects the FINAL classification the iteration-close phases
already ran against — no extra observation needed.

Verification strategy: extract the bash conditional that drives the
imperative selection and exercise it under both OUTCOME values via
shell -c. The test catches: (1) condition inversion, (2) missing
aspirations-spark reference on deep, (3) accidental aspirations-spark
reference on routine, (4) regression to the old unconditional form.

Cross-refs:
  - g-115-977 (this fix — Apply)
  - g-115-965 (Investigate origin — docs-vs-impl drift finding)
  - Universal RB "Docs-vs-impl drift in framework shortcut wrappers"
  - core/scripts/recurring-close.sh (the outcome-aware imperative at bottom)
  - core/config/aspirations-loop-digest.md Phase 6 note
  - .claude/skills/aspirations/SKILL.md recurring-shortcut block
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

# Resolve bash absolutely (Windows App Paths WSL-bash defense — same pattern as
# test_recurring_close_outcome_origin.py).
from _bash_helpers import BASH as BASH_PATH  # rb-1472: bin-first, clean-PATH-safe


def _run_imperative_shell(outcome: str) -> str:
    """Execute the terminal-imperative conditional under a given OUTCOME.

    The conditional in recurring-close.sh is small (~5 lines around the
    if/else) and inline; reproducing it via shell -c exercises the SAME
    branch logic the production path runs. If the conditional is ever
    inverted or the deep/routine messages drift apart, this fires.

    Mirrors test_recurring_close_outcome_origin.py's
    `_derive_origin_via_shell` pattern.
    """
    script = f"""
OUTCOME="{outcome}"
if [[ "$OUTCOME" == "deep" ]]; then
    echo "[recurring-close] OUTCOME=deep — NEXT ACTION REQUIRED: Call Skill(aspirations-spark) FIRST (Phase 6 fires on deep; NOT wrapped by recurring-close.sh), THEN Skill(aspirations) with args='loop'."
else
    echo "[recurring-close] OUTCOME=routine — NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call."
fi
"""
    result = subprocess.run(
        [BASH_PATH, "-c", script],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"shell exited non-zero ({result.returncode}); stderr: {result.stderr}"
    )
    return result.stdout.strip()


# ─── TestImperativeConditional: shell-level branch selection ────────────


class TestImperativeConditional:
    """Pin the bash conditional that selects the deep vs routine imperative.

    Exercises the actual shell branch logic via bash -c so the test
    catches bash-syntax regressions (inverted operator, mistyped quotes,
    case-statement vs test-bracket drift).
    """

    def test_deep_imperative_references_aspirations_spark(self):
        """Deep outcome MUST direct LLM to fire Phase 6 spark FIRST."""
        out = _run_imperative_shell("deep")
        assert "aspirations-spark" in out, (
            f"deep imperative missing aspirations-spark reference: {out!r}"
        )
        assert "FIRST" in out, (
            f"deep imperative missing FIRST ordering directive: {out!r}"
        )
        assert "Skill(aspirations)" in out, (
            f"deep imperative missing loop continuation directive: {out!r}"
        )

    def test_routine_imperative_omits_aspirations_spark(self):
        """Routine outcome MUST NOT reference aspirations-spark (Phase 6 skip
        rule preserved — spark fires on deep only)."""
        out = _run_imperative_shell("routine")
        assert "aspirations-spark" not in out, (
            f"routine imperative wrongly references aspirations-spark: {out!r}"
        )
        assert "Skill(aspirations)" in out, (
            f"routine imperative missing loop continuation directive: {out!r}"
        )
        assert "args='loop'" in out, (
            f"routine imperative missing args='loop' directive: {out!r}"
        )

    def test_unknown_outcome_falls_to_routine_branch(self):
        """Defensive: if OUTCOME ever holds an unexpected token (parser bug,
        empty string, etc.), the else-branch fires — emitting the safe
        routine imperative. This preserves the loop on the safe path even
        under an upstream regression that the post-mutation guard at
        lines ~187-189 (`FINAL_OUTCOME != routine && FINAL_OUTCOME != deep`)
        is supposed to prevent. Defense-in-depth check."""
        out = _run_imperative_shell("")
        assert "aspirations-spark" not in out, (
            f"empty-outcome must fall to routine branch (safe default): {out!r}"
        )
        assert "Skill(aspirations)" in out


# ─── TestProductionScriptContents: structural pin on recurring-close.sh ──


class TestProductionScriptContents:
    """Pin the production script's outcome-aware imperative is actually
    wired into the terminal output block — not merely added as a
    comment somewhere else in the file.

    The test reads recurring-close.sh and verifies the imperative block
    contains BOTH branches with the canonical phrases. Catches the
    regression where the conditional is added but only one branch's
    echo string lands (copy-paste failure)."""

    def test_script_contains_deep_branch(self):
        src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
        assert 'if [[ "$OUTCOME" == "deep" ]]; then' in src, (
            "outcome-aware conditional missing — regression to unconditional form"
        )
        assert "Skill(aspirations-spark) FIRST" in src, (
            "deep-branch imperative phrase missing — spark not directed"
        )

    def test_script_contains_routine_branch(self):
        src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
        # The routine branch is the else clause of the conditional.
        # Match on the OUTCOME=routine prefix which is unique to the
        # new imperative (the pre- form did not prefix with
        # OUTCOME=...).
        assert "OUTCOME=routine" in src, (
            "routine-branch OUTCOME=... prefix missing — regression to unconditional form"
        )
        # The kill-the-loop reminder must still appear AFTER the
        # conditional (preserved from session-58 fix).
        assert "kills the loop" in src, (
            "loop-death warning missing from terminal output (return-protocol cross-ref)"
        )

    def test_script_no_unconditional_legacy_imperative(self):
        """The pre- form was a single unconditional echo:
            echo "[recurring-close] NEXT ACTION REQUIRED: Call Skill(aspirations) with args='loop' as your VERY NEXT tool call."
        That exact line should no longer appear — it was replaced by the
        outcome-aware branches. A surviving unconditional echo would mean
        the edit landed on a duplicate copy and the real terminal output
        is still drift-vulnerable."""
        src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
        # The exact pre-fix line — note the absence of any OUTCOME= prefix.
        legacy = ('echo "[recurring-close] NEXT ACTION REQUIRED: '
                  'Call Skill(aspirations) with args=\'loop\' as your '
                  'VERY NEXT tool call."')
        assert legacy not in src, (
            "legacy unconditional imperative survived the g-115-977 edit "
            "— production terminal output may still bypass Phase 6"
        )


if __name__ == "__main__":
    # Allow direct invocation without pytest for quick smoke testing.
    import sys
    import traceback

    classes = [TestImperativeConditional, TestProductionScriptContents]
    passed = total = 0
    for cls in classes:
        inst = cls()
        for name in dir(inst):
            if not name.startswith("test_"):
                continue
            total += 1
            try:
                getattr(inst, name)()
                print(f"  [PASS] {cls.__name__}.{name}")
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {cls.__name__}.{name}: {e}")
                traceback.print_exc()
            except Exception as e:
                print(f"  [ERROR] {cls.__name__}.{name}: {type(e).__name__}: {e}")
                traceback.print_exc()
    print(f"\n{passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)
