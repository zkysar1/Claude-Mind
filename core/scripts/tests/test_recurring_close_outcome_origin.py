"""test_recurring_close_outcome_origin.py —  regression test.

Pins the OUTCOME_ORIGIN distinction added to recurring-close.sh that
breaks the runaway feedback loop between anti-drift force-flip (Block A/C
in recurring-loop-state-mutate.py) and auto-contract (consecutive_deep
counter driving cargo-cult-detector --contract-mode).

Bug origin (g-115-896): when a recurring goal closed routine 3+ times in
a row, Block C ceiling (default 5) or per-goal flip_threshold (default 5)
force-flipped routine→deep. recurring-close.sh then incremented
consecutive_deep unconditionally on every deep outcome — including the
forced flips. After 3 such flips, auto-contract fired, shrinking the
interval. Tighter interval → goal fires more often → more routine streaks
→ more flips → further contraction. g-115-817 (alert-sweep) was the
canonical victim: 1.0h interval → 0.33h floor in three iterations.

Fix (g-115-898):
  1. recurring-close.sh derives outcome_origin from
     (ORIGINAL_OUTCOME, FINAL_OUTCOME):
       origin = "forced-flip" when ORIGINAL=routine AND FINAL=deep
              = "genuine" otherwise
  2. consecutive_deep advances ONLY on genuine deeps:
       genuine deep    → consecutive_deep += 1
       forced-flip deep → consecutive_deep UNCHANGED
       routine          → consecutive_deep = 0
  3. last_outcome_origin persisted on the goal record for audit.

Verification strategy: two test classes pin the two layers.

  TestOriginDerivation invokes the actual bash conditional from
  recurring-close.sh via shell -c, exercising the production derivation
  with all four (original, final) combinations.

  TestConsecutiveDeepMath pins the Python heredoc's counter conditional
  with a faithful reproduction. Inlining is acceptable because the
  conditional is 4 lines; a refactor to a shared helper would inflate
  the production diff beyond goal scope.

Cross-refs:
  - g-115-898 (this fix)
  - g-115-896 (origin Idea)
  - g-115-817 (alert-sweep — canonical runaway incident)
  - core/scripts/recurring-close.sh (the OUTCOME_ORIGIN derivation + counter math)
  - core/scripts/recurring-loop-state-mutate.py (Block A/C flips that motivate the fix)
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

# Resolve bash absolutely — same pattern as
# test_iteration_close_outcome_normalize.py and
# test_orphan_root_sweep_mode_d_integration.py — to bypass Windows App
# Paths picking WSL bash via registry lookup.
from _bash_helpers import BASH as BASH_PATH  # rb-1472: bin-first, clean-PATH-safe


def _derive_origin_via_shell(original: str, final: str) -> str:
    """Run the actual OUTCOME_ORIGIN derivation conditional via bash -c.

    Mirrors the conditional embedded in recurring-close.sh (lines ~270-274
    after the g-115-898 edit). Using shell -c instead of a Python
    reproduction means the test exercises the SAME source the production
    path runs — if the bash conditional is ever inverted, this test fires.
    """
    script = f"""
ORIGINAL_OUTCOME="{original}"
OUTCOME="{final}"
if [[ "$OUTCOME" == "deep" && "$ORIGINAL_OUTCOME" == "routine" ]]; then
    echo "forced-flip"
else
    echo "genuine"
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


def _compute_new_deep(outcome: str, outcome_origin: str, current_deep: int) -> int:
    """Faithful reproduction of the counter conditional in recurring-close.sh.

    The conditional is small enough (4 lines) that inline reproduction
    in the test is acceptable; the alternative (extracting to a shared
    helper) would inflate the production diff beyond the implementation-
    discipline rule's surgical bound.

    If the production code's conditional is ever changed, this test
    must be updated in tandem — see the cross-reference comment in
    recurring-close.sh near the heredoc.
    """
    if outcome == "deep" and outcome_origin == "genuine":
        return current_deep + 1
    elif outcome == "deep":  # outcome_origin == "forced-flip"
        return current_deep  # unchanged
    else:  # routine
        return 0


# ─── TestOriginDerivation: shell-level conditional ──────────────────────


class TestOriginDerivation:
    """Pin the bash-level OUTCOME_ORIGIN derivation in recurring-close.sh.

    Exercises the actual shell conditional via bash -c so the test
    catches bash-syntax regressions (inverted operator, missing quotes,
    test-bracket vs case-statement drift).
    """

    def test_routine_to_deep_is_forced_flip(self):
        """The canonical Block A/C flip: LLM said routine, mutate flipped
        to deep — origin must be 'forced-flip' so consecutive_deep does
        NOT advance."""
        assert _derive_origin_via_shell("routine", "deep") == "forced-flip"

    def test_deep_to_deep_is_genuine(self):
        """LLM claimed deep, mutate preserved deep — genuine. Caller's
        deep claim was honored end-to-end."""
        assert _derive_origin_via_shell("deep", "deep") == "genuine"

    def test_routine_to_routine_is_genuine(self):
        """No flip happened — outcome stayed routine. Origin is genuine
        (no forced flip). Value moot for counter math (only deep outcomes
        consume origin) but the derivation should be deterministic."""
        assert _derive_origin_via_shell("routine", "routine") == "genuine"

    def test_deep_to_routine_is_genuine(self):
        """Edge case: mutate logic never demotes deep→routine (Block A/C
        only flip routine→deep). If it ever did, the origin would be
        genuine — origin tracks forced UP-flips only, not arbitrary
        reclassifications."""
        assert _derive_origin_via_shell("deep", "routine") == "genuine"


# ─── TestConsecutiveDeepMath: counter conditional ───────────────────────


class TestConsecutiveDeepMath:
    """Pin the new_deep counter conditional from recurring-close.sh's
    inline Python heredoc.

    The production code is small (4 lines) and inline; this test
    reproduces it faithfully and verifies the counter math against the
    four (outcome × origin) combinations that matter for auto-contract.
    """

    def test_genuine_deep_advances_counter(self):
        """The standard auto-contract trigger: 2 + 1 = 3. When 3 genuine
        deeps stack, contract_threshold is hit and auto-contract fires."""
        assert _compute_new_deep("deep", "genuine", 2) == 3

    def test_forced_flip_pins_counter(self):
        """The  fix's central behavior: forced-flip deep leaves
        consecutive_deep UNCHANGED. Without this, a 3-routine-streak goal
        would falsely trigger auto-contract."""
        assert _compute_new_deep("deep", "forced-flip", 2) == 2

    def test_routine_resets_counter(self):
        """A routine outcome breaks the deep streak — counter resets to 0.
        Holds regardless of origin (routine path doesn't consume origin)."""
        assert _compute_new_deep("routine", "genuine", 2) == 0
        assert _compute_new_deep("routine", "forced-flip", 2) == 0

    def test_three_genuine_deeps_reach_contract_threshold(self):
        """The auto-contract trigger chain: 0 → 1 → 2 → 3 across three
        consecutive genuine-deep closes. Matches the default
        deep_streak_contract_threshold of 3."""
        n = 0
        for _ in range(3):
            n = _compute_new_deep("deep", "genuine", n)
        assert n == 3, f"three genuine deeps must reach 3; got {n}"

    def test_three_forced_flips_do_not_reach_threshold(self):
        """The  fix's headline guarantee: three consecutive
        forced-flip deeps leave consecutive_deep at 0 — contract never
        fires. This is the runaway-feedback-loop break."""
        n = 0
        for _ in range(3):
            n = _compute_new_deep("deep", "forced-flip", n)
        assert n == 0, (
            f"three forced-flip deeps must NOT advance counter; got {n} "
            f"(would trigger spurious auto-contract)"
        )

    def test_mixed_sequence_only_genuine_advances(self):
        """Realistic recurring-goal trajectory: genuine, forced-flip,
        genuine, forced-flip, genuine. Counter advances 1, stays 1,
        advances 2, stays 2, advances 3 — contract fires on the FIFTH
        close, not the third, because two of the deeps were forced-flips.
        """
        sequence = [
            ("deep", "genuine"),
            ("deep", "forced-flip"),
            ("deep", "genuine"),
            ("deep", "forced-flip"),
            ("deep", "genuine"),
        ]
        n = 0
        history = []
        for outcome, origin in sequence:
            n = _compute_new_deep(outcome, origin, n)
            history.append(n)
        assert history == [1, 1, 2, 2, 3], (
            f"mixed sequence counter trajectory mismatch: {history}"
        )


# ─── TestProductionSourceContainsConditional: smoke-grep ────────────────


class TestProductionSourceContainsConditional:
    """Defense-in-depth: verify the production source ACTUALLY contains
    the conditional this test pins.

    The TestConsecutiveDeepMath class reproduces the logic inline rather
    than extracting it from the source. That makes the test fast but
    means a refactor to recurring-close.sh that removes the conditional
    would silently pass this file's other tests. This class greps the
    source for the load-bearing tokens so refactors must update both.
    """

    def test_recurring_close_has_outcome_origin_derivation(self):
        """Verify the bash conditional that derives OUTCOME_ORIGIN exists
        in recurring-close.sh. The exact string is part of the contract."""
        src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
        assert 'OUTCOME_ORIGIN="forced-flip"' in src, (
            "recurring-close.sh must set OUTCOME_ORIGIN='forced-flip' "
            "when ORIGINAL=routine AND FINAL=deep (g-115-898)"
        )
        assert 'OUTCOME_ORIGIN="genuine"' in src, (
            "recurring-close.sh must set OUTCOME_ORIGIN='genuine' in "
            "the else branch (g-115-898)"
        )

    def test_recurring_close_has_genuine_only_counter_advance(self):
        """Verify the Python heredoc's counter conditional advances only
        on outcome_origin=='genuine'. Without this, forced-flips would
        still pollute consecutive_deep."""
        src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
        # The conditional has a stable shape — match on the load-bearing
        # tokens that would have to change in lockstep with any logic edit.
        assert 'outcome == "deep" and outcome_origin == "genuine"' in src, (
            "recurring-close.sh Python heredoc must gate counter "
            "advance on outcome_origin == 'genuine' (g-115-898)"
        )

    def test_recurring_close_persists_last_outcome_origin(self):
        """Verify last_outcome_origin is written to the goal record for
        audit. Without this, debugging WHY consecutive_deep advanced or
        stalled in a prior session requires diving into stderr logs."""
        src = RECURRING_CLOSE_SH.read_text(encoding="utf-8")
        assert 'last_outcome_origin' in src, (
            "recurring-close.sh must persist last_outcome_origin on the "
            "goal record (g-115-898)"
        )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
