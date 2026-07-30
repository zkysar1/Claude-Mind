"""test_recurring_close_quality_flags.py —  regression test.

Pins the four § STATE-UPDATE quality flags on recurring-close.sh.

Bug origin: iteration-close.sh has parsed --tree-updated /
--tree-updated-override / --artifacts-count / --encoding-score /
--findings-count since g-115-228 (iteration-close.sh:179-186), but
recurring-close.sh's own parser accepted only --source / --summary /
--outcome / --goal / --override-uncommitted. Its `-*)` catch-all exited rc=2
"unknown flag", so a caller closing a RECURRING deep goal could not pass any
of them, and the state-update dispatch forwarded only COMMON + --outcome.

Precise scope of what was broken (measured 2026-07-28 — the goal's own
framing over-stated it, so it is pinned here):

  * --tree-updated was ALREADY reachable on the recurring path, via the
    g-273-20 auto-detect inside iteration-close.sh do_state_update (probes
    iteration-checkpoint.json:selected_at against tree .md mtimes). Since
    state-update-audit.py sets `measured = bool(tree_updated) or any(...)`,
    a recurring deep close that edited the tree BEFORE the wrapper ran was
    already measured. Forwarding it explicitly is still worth doing: the
    claim becomes auditable, and the g-115-464 validator still IGNORES it
    (loudly) when no tree edit is detectable since the anchor.
  * The three VALUE flags had no auto-detect and no parser entry, so they
    were genuinely unreachable — the close could be measured but never
    enriched with artifact/encoding/finding counts.

Fix: add the five flags to the parser and forward them via a STATE_EXTRA
array on the state-update dispatch ONLY.

The COMMON-contamination trap this test guards (case 3): verify and
learning-gate reject unknown flags, so adding these to COMMON instead of a
phase-specific array would break EVERY recurring close, routine and deep
alike — a far worse regression than the gap being fixed.

Cross-refs:
  - g-115-3192 (this fix), g-115-228 (iteration-close flag origin)
  - guard-1235 (pass the flags on the FIRST state-update call; no post-hoc
    amend — re-running re-fires journal-append + iteration-commit)
  - core/scripts/recurring-close.sh (parser + STATE_EXTRA + dispatch)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
RECURRING_CLOSE_SH = CORE_SCRIPTS / "recurring-close.sh"

from _bash_helpers import BASH as BASH_PATH  # rb-1472 / guard-580: never bare "bash"

QUALITY_FLAGS = [
    "--tree-updated",
    "--tree-updated-override",
    "--artifacts-count",
    "--encoding-score",
    "--findings-count",
]

VALUE_FLAGS = ["--artifacts-count", "--encoding-score", "--findings-count"]


def _script_text() -> str:
    return RECURRING_CLOSE_SH.read_text(encoding="utf-8")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Invoke the wrapper with args that stop at the usage guard.

    Every invocation here omits at least one required field (goal-id /
    outcome / source), so the script exits at its usage check BEFORE
    touching any store. Safe to run in-suite: no goal is closed, no
    aspiration file is written.
    """
    return subprocess.run(
        [BASH_PATH, RECURRING_CLOSE_SH.as_posix(), *args],
        capture_output=True,
        text=True,
        cwd=CORE_SCRIPTS.parent.parent.as_posix(),
    )


# ── Case 1: the parser accepts every quality flag ────────────────────────
# Pre-fix these hit the `-*)` catch-all -> "unknown flag" on stderr, rc=2.


def test_each_quality_flag_is_not_rejected_as_unknown():
    for flag in QUALITY_FLAGS:
        args = [flag, "5"] if flag in VALUE_FLAGS else [flag]
        res = _run(args)
        combined = res.stdout + res.stderr
        assert "unknown flag" not in combined, (
            f"{flag} was rejected by the parser: {combined!r}"
        )


def test_all_quality_flags_together_are_accepted():
    res = _run([
        "--tree-updated",
        "--tree-updated-override",
        "--artifacts-count", "5",
        "--encoding-score", "0.9",
        "--findings-count", "3",
    ])
    combined = res.stdout + res.stderr
    assert "unknown flag" not in combined, combined
    # Falls through to the usage guard because goal-id/outcome/source are absent.
    assert "Usage: recurring-close.sh" in combined, combined


def test_unknown_flag_is_still_rejected():
    """The catch-all must survive — this fix widens the parser, not opens it."""
    res = _run(["--not-a-real-flag"])
    combined = res.stdout + res.stderr
    assert "unknown flag" in combined, (
        f"catch-all no longer rejects unknown flags: {combined!r}"
    )


# ── Case 2: STATE_EXTRA is built correctly and only from supplied flags ──


def _state_extra_fragment() -> str:
    """Extract the STATE_EXTRA build block from the live script."""
    text = _script_text()
    start = text.index("STATE_EXTRA=()")
    end = text.index("run_phase verify", start)
    return text[start:end]


def _eval_state_extra(env_assignments: str) -> list[str]:
    """Run the real STATE_EXTRA block under given inputs; return the array."""
    fragment = _state_extra_fragment()
    script = (
        "set -uo pipefail\n"
        f"{env_assignments}\n"
        f"{fragment}\n"
        'printf "%s\\n" "${STATE_EXTRA[@]+"${STATE_EXTRA[@]}"}"\n'
    )
    res = subprocess.run(
        [BASH_PATH, "-c", script], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    return [ln for ln in res.stdout.splitlines() if ln]


ALL_UNSET = (
    'TREE_UPDATED=""\nTREE_UPDATED_OVERRIDE=""\n'
    'ARTIFACTS_COUNT=""\nENCODING_SCORE=""\nFINDINGS_COUNT=""'
)


def test_state_extra_empty_when_no_flags_supplied():
    """Byte-identical to pre-fix behavior: omitted flags add nothing."""
    assert _eval_state_extra(ALL_UNSET) == []


def test_state_extra_carries_only_supplied_flags():
    env = (
        'TREE_UPDATED="true"\nTREE_UPDATED_OVERRIDE=""\n'
        'ARTIFACTS_COUNT="5"\nENCODING_SCORE=""\nFINDINGS_COUNT="3"'
    )
    out = _eval_state_extra(env)
    assert out == [
        "--tree-updated",
        "--artifacts-count", "5",
        "--findings-count", "3",
    ], out


def test_state_extra_full_set():
    env = (
        'TREE_UPDATED="true"\nTREE_UPDATED_OVERRIDE="true"\n'
        'ARTIFACTS_COUNT="5"\nENCODING_SCORE="0.9"\nFINDINGS_COUNT="3"'
    )
    out = _eval_state_extra(env)
    assert out == [
        "--tree-updated",
        "--tree-updated-override",
        "--artifacts-count", "5",
        "--encoding-score", "0.9",
        "--findings-count", "3",
    ], out


def test_tree_updated_requires_literal_true():
    """Guards a truthiness slip: only the literal "true" emits the flag."""
    env = (
        'TREE_UPDATED="false"\nTREE_UPDATED_OVERRIDE=""\n'
        'ARTIFACTS_COUNT=""\nENCODING_SCORE=""\nFINDINGS_COUNT=""'
    )
    assert _eval_state_extra(env) == []


def test_zero_valued_counts_are_forwarded():
    """0 is a real measurement (e.g. findings-count 0), not an absent flag.

    -n on the string "0" is true, so this passes by construction today. Pinned
    because switching the guard to an arithmetic test would silently drop
    legitimate zero measurements.
    """
    env = (
        'TREE_UPDATED=""\nTREE_UPDATED_OVERRIDE=""\n'
        'ARTIFACTS_COUNT="0"\nENCODING_SCORE="0"\nFINDINGS_COUNT="0"'
    )
    out = _eval_state_extra(env)
    assert out == [
        "--artifacts-count", "0",
        "--encoding-score", "0",
        "--findings-count", "0",
    ], out


# ── Case 3: forwarded to state-update ONLY (COMMON-contamination guard) ──


def _dispatch_line(phase: str) -> str:
    for line in _script_text().splitlines():
        if line.startswith(f"run_phase {phase} "):
            return line
    raise AssertionError(f"no run_phase dispatch line found for {phase!r}")


def test_state_update_dispatch_forwards_state_extra():
    line = _dispatch_line("state-update")
    assert "STATE_EXTRA[@]" in line, line


def test_verify_dispatch_does_not_carry_state_extra():
    assert "STATE_EXTRA" not in _dispatch_line("verify")


def test_learning_gate_dispatch_does_not_carry_state_extra():
    assert "STATE_EXTRA" not in _dispatch_line("learning-gate")


def test_common_array_does_not_carry_quality_flags():
    """The load-bearing guard: quality flags in COMMON would break verify and
    learning-gate, which reject unknown flags — every recurring close, not
    just deep ones."""
    text = _script_text()
    m = re.search(r"^COMMON=\((.*)\)$", text, re.MULTILINE)
    assert m, "COMMON array assignment not found"
    common = m.group(1)
    for flag in QUALITY_FLAGS:
        assert flag not in common, (
            f"{flag} leaked into COMMON — verify/learning-gate will reject it"
        )
