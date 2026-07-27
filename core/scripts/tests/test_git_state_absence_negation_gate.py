#!/usr/bin/env python3
"""test_git_state_absence_negation_gate.py —  regression test.

Verifies the git-state-absence negation family fires BOTH the
exhaustive-search gate and the verify-before-assuming gate.

Background (g-248-116): both gates already trigger on the capability-absence
family ("isn't built", "doesn't exist", "can't be done", "no such file"), but
NEITHER matched the version-control-missing variant of the same claim —
"X is absent from git", "not committed", "exists only in Studio", "not tracked
in git". That gap had a measured, cost-bearing failure: goal g-350-63 carried a
defer finding "InteractNearby/action_interact ABSENT from git, Studio-only"
(2026-07-20) which was FALSE at HEAD (the code was committed 2026-07-15) and
FROZE the goal for ~2 days — exactly the false-negation class these gates exist
to catch. The classifier audit (g-248-28,
agents/foxtrot/temp/classifier-accuracy-2026-07-22.md) flagged this as a 100%
miss on that pattern class (sites 4 + 10).

Invariant tested: every git-state-absence phrase triggers _detect_trigger in
BOTH gates (returns a non-None matched substring), while a benign sentence that
merely mentions git does NOT trigger (no false positive from the word "git"
alone).

Run: py -3 core/scripts/tests/test_git_state_absence_negation_gate.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
# The gate modules import sibling helpers (_gate_log, _paths) at module load —
# core/scripts must be importable.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _load_detect_trigger(script_name: str, module_name: str):
    """Import _detect_trigger from a hyphenated gate script by path."""
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPT_DIR / script_name
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._detect_trigger


_exhaustive_detect = _load_detect_trigger(
    "exhaustive-search-gate.py", "exhaustive_search_gate"
)
_verify_detect = _load_detect_trigger(
    "verify-before-assuming-gate.py", "verify_before_assuming_gate"
)

# The git-state-absence negation family (). Each MUST trigger both
# gates. Mixed case is deliberate — the patterns are re.IGNORECASE.
GIT_ABSENCE_CLAIMS = [
    "The InteractNearby deliverable is absent from git, Studio-only.",
    "This handler is not in git yet.",
    "That change is not in git.",
    "The E-to-interact path was not committed.",
    "This script was never committed.",
    "The HUD exists only in Studio.",
    "The chat UI exists only in studio.",
    "That module is Studio-only.",
    "The prompt is a studio-only asset.",
    "The scenario file is not tracked in git.",
    "The seed is not tracked by git.",
    # : alternate phrasings of the SAME class — contraction / repo
    # synonym / passive voice / "pushed" / word-order variants — that STILL
    # escaped after  added the base forms above (found by the 
    # site-#4 FN re-review). Each MUST trigger BOTH gates.
    "The handler isn't in git.",
    "That code is not in the repo.",
    "The change hasn't been committed.",
    "It was never pushed to git.",
    "The branch hasn't been pushed.",
    "The file is missing from the repo.",
    "The handler only exists in Studio.",
]

# Benign sentences that mention git/Studio but make NO absence claim — must NOT
# trigger (guard against the word "git" alone false-positiving).
BENIGN_CLAIMS = [
    "The InteractNearby deliverable is committed in git and present at HEAD.",
    "I pushed the fix to git and CI applied it.",
    "The scenario file is tracked in git and current.",
    #  FP controls guarding the new patterns from over-triggering:
    # "never pushed" has a strong non-git sense (a UI button), so the pattern
    # requires an explicit git target — a bare button-push must NOT trigger;
    # "in the repo" fires only under negation, so a positive mention must NOT.
    "I never pushed the button on the UI.",
    "The handler is in the repo already.",
]


def test_git_absence_triggers_exhaustive_search_gate():
    for claim in GIT_ABSENCE_CLAIMS:
        matched = _exhaustive_detect(claim)
        assert matched is not None, (
            f"exhaustive-search-gate did NOT trigger on git-absence claim: {claim!r}"
        )


def test_git_absence_triggers_verify_before_assuming_gate():
    for claim in GIT_ABSENCE_CLAIMS:
        matched = _verify_detect(claim)
        assert matched is not None, (
            f"verify-before-assuming-gate did NOT trigger on git-absence claim: {claim!r}"
        )


def test_benign_git_mentions_do_not_trigger_exhaustive():
    for claim in BENIGN_CLAIMS:
        matched = _exhaustive_detect(claim)
        assert matched is None, (
            f"exhaustive-search-gate FALSE-POSITIVE on benign claim: {claim!r} "
            f"(matched {matched!r})"
        )


def test_benign_git_mentions_do_not_trigger_verify():
    # verify-before-assuming has a broader infra pattern set; only assert on the
    # benign git-specific sentences, which carry no infra-negation phrasing.
    for claim in BENIGN_CLAIMS:
        matched = _verify_detect(claim)
        assert matched is None, (
            f"verify-before-assuming-gate FALSE-POSITIVE on benign claim: {claim!r} "
            f"(matched {matched!r})"
        )


if __name__ == "__main__":
    test_git_absence_triggers_exhaustive_search_gate()
    test_git_absence_triggers_verify_before_assuming_gate()
    test_benign_git_mentions_do_not_trigger_exhaustive()
    test_benign_git_mentions_do_not_trigger_verify()
    print("PASS: git-state-absence negation triggers both gates; no benign FPs "
          f"({len(GIT_ABSENCE_CLAIMS)} absence claims × 2 gates + "
          f"{len(BENIGN_CLAIMS)} benign × 2 gates)")
