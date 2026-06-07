"""test_iteration_close_outcome_normalize.py -  regression test.

Pins the --outcome short-form normalization in iteration-close.sh:
  d        -> deep
  r        -> routine
  deep     -> deep (passthrough)
  routine  -> routine (passthrough)
  <other>  -> exit 2 with "invalid --outcome"

Origin of bug (echo session 2026-05-17 g-315-34): iteration-close.sh's
top-level help advertised --outcome <d|r> short forms, but every downstream
consumer (loop-state-bump-counters.py argparse choices=routine,deep;
aspirations-update-goal outcome_class field; comparisons against the
literal string "deep") expected the long form. A caller following the
documented signature received:

    loop-state-bump-counters.py: error: argument --outcome: invalid choice:
    'd' (choose from routine, deep)

The fail-open WARN line in iteration-close.sh masked the failure;
goals_completed_this_session stayed stale, productivity-gate underestimated.

Fix (g-115-881): translate short forms to canonical full forms at
argument-parse time so every downstream consumer (10+ call sites)
sees the canonical form regardless of caller convention.

Test strategy: the normalize block runs BEFORE the --phase / MIND_AGENT
required-arg checks. Invoking iteration-close.sh with --outcome but NO
--phase exits at the PHASE check (usage error, rc=2) for valid OUTCOME
values, and at the normalize block (rc=2 with "invalid --outcome") for
invalid values. Stderr content distinguishes the two paths — no project
state mutation needed.

Refs: g-115-881 (this fix), g-315-34 (the close that surfaced it),
core/scripts/iteration-close.sh (the normalize block).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # core/scripts/
PROJECT_ROOT = SCRIPT_DIR.parent
ITERATION_CLOSE = SCRIPT_DIR / "iteration-close.sh"

# Resolve bash absolutely so subprocess.run on Windows doesn't pick WSL
# bash via App Paths registry lookup. Same pattern as
# test_orphan_root_sweep_mode_d_integration.py.
from _bash_helpers import BASH as BASH_PATH  # rb-1472: bin-first, clean-PATH-safe


def _invoke(outcome: str | None) -> subprocess.CompletedProcess:
    """Run iteration-close.sh with the given --outcome and NO --phase.

    Strategy: no --phase means the script exits at the PHASE-required
    check (line ~211, post-fix) for VALID outcomes (normalize passed
    through), and at the normalize block itself (line ~205, post-fix)
    for INVALID outcomes. Both yield rc=2 — distinguish by stderr.

    We use the REAL iteration-close.sh (absolute path) so the script's
    own PROJECT_ROOT resolution works. MIND_AGENT is unset deliberately
    — the AGENT check runs AFTER both the normalize and PHASE checks,
    so neither code path under test depends on it.
    """
    env = os.environ.copy()
    # Strip MIND_* env so the script doesn't pick up host session bindings.
    # The normalize/PHASE checks both run before AGENT_DIR resolution.
    for k in list(env):
        if k.startswith("MIND_"):
            env.pop(k, None)
    args = [BASH_PATH, str(ITERATION_CLOSE)]
    if outcome is not None:
        args.extend(["--outcome", outcome])
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=15,
    )


# ── Positive cases — valid forms reach the PHASE-required check ─────────


def test_outcome_d_short_form_accepted() -> None:
    """`--outcome d` is normalized at parse time. Without --phase, the
    script exits at the PHASE check (usage error), NOT at the normalize
    block. Stderr contains the PHASE usage message, NOT 'invalid
    --outcome'. The downstream argparse signature 'invalid choice: d'
    is impossible here (loop-state-bump-counters never runs without
    --phase), but its absence still confirms the normalize fired."""
    result = _invoke(outcome="d")
    assert result.returncode == 2, (
        f"missing --phase yields rc=2; got rc={result.returncode}; "
        f"stderr: {result.stderr[-400:]}"
    )
    # Normalize block did NOT reject — short form 'd' was accepted.
    assert "invalid --outcome" not in result.stderr, (
        f"normalize-block rejection should NOT fire for short form 'd'; "
        f"stderr: {result.stderr[-400:]}"
    )
    # Script reached the PHASE-required check (proves normalize fell through).
    assert "usage: iteration-close.sh --phase" in result.stderr, (
        f"should reach PHASE-required check; stderr: {result.stderr[-400:]}"
    )


def test_outcome_r_short_form_accepted() -> None:
    """`--outcome r` symmetric with d -> deep test."""
    result = _invoke(outcome="r")
    assert result.returncode == 2
    assert "invalid --outcome" not in result.stderr
    assert "usage: iteration-close.sh --phase" in result.stderr


def test_outcome_deep_long_form_passthrough() -> None:
    """`--outcome deep` accepted without modification (no regression on
    callers using the canonical long form, e.g. recurring-close.sh)."""
    result = _invoke(outcome="deep")
    assert result.returncode == 2
    assert "invalid --outcome" not in result.stderr
    assert "usage: iteration-close.sh --phase" in result.stderr


def test_outcome_routine_long_form_passthrough() -> None:
    """`--outcome routine` accepted (symmetric with deep passthrough)."""
    result = _invoke(outcome="routine")
    assert result.returncode == 2
    assert "invalid --outcome" not in result.stderr
    assert "usage: iteration-close.sh --phase" in result.stderr


# ── Negative cases — invalid forms rejected by normalize block ──────────


def test_outcome_invalid_form_rejected() -> None:
    """`--outcome xxx` (not d/r/deep/routine) is rejected with the new
    parse-time error message, BEFORE the PHASE check runs.

    The error message names all 4 accepted forms so caller can choose
    either short or long convention.
    """
    result = _invoke(outcome="xxx")
    assert result.returncode == 2, (
        f"--outcome xxx should be rejected (rc=2); got rc={result.returncode}; "
        f"stderr: {result.stderr[-400:]}"
    )
    assert "invalid --outcome 'xxx'" in result.stderr, (
        f"normalize-block error message should include the offending value; "
        f"stderr: {result.stderr[-400:]}"
    )
    # The error message advertises BOTH short and long forms as accepted.
    assert "deep|routine|d|r" in result.stderr, (
        f"error message should name all 4 accepted forms; stderr: {result.stderr[-400:]}"
    )
    # Normalize fired BEFORE the PHASE-required check, so the PHASE usage
    # message should NOT appear (normalize exits 2 first).
    assert "usage: iteration-close.sh --phase" not in result.stderr, (
        f"normalize should exit BEFORE PHASE check; stderr: {result.stderr[-400:]}"
    )


def test_outcome_uppercase_rejected() -> None:
    """`--outcome DEEP` (uppercase) is rejected — the normalize is
    case-sensitive on purpose (downstream consumers compare against the
    literal lowercase strings 'deep' and 'routine')."""
    result = _invoke(outcome="DEEP")
    assert result.returncode == 2, (
        f"--outcome DEEP should be rejected (rc=2); got rc={result.returncode}; "
        f"stderr: {result.stderr[-400:]}"
    )
    assert "invalid --outcome 'DEEP'" in result.stderr


# ── Backward compatibility — --outcome omitted ──────────────────────────


def test_outcome_omitted_skips_normalize() -> None:
    """Callers that omit --outcome entirely (e.g. verify-only paths,
    productivity-check phase) skip the normalize block. The PHASE
    usage check still fires (no --phase passed), but 'invalid --outcome'
    must NOT appear — the normalize block guards on `-n "$OUTCOME"`."""
    result = _invoke(outcome=None)
    assert result.returncode == 2
    assert "invalid --outcome" not in result.stderr
    assert "usage: iteration-close.sh --phase" in result.stderr


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
