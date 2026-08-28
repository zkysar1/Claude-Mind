"""Pin : the state-update RECOVERY retry line must carry THIS call's quality flags.

`_quality_flag_suffix()` (iteration-close.sh:263) builds
" --tree-updated --artifacts-count N --encoding-score X --findings-count N" from the
values already in scope, and the state-update branch of `_print_recovery_instructions`
appends it to every retry line it emits. Without it a caller who passed correct flags
got a retry line at argparse defaults, so the rerun hit the UNMEASURED advisory and the
imp@k hole opened on the DEFAULT path after any failed state-update block.

WHY THE FIXTURE IS A **COMPLETED** GOAL, AND WHY THAT IS THE WHOLE TEST (g-115-7752 (d)).
The suffix carry lives inside `case "$_live" in completed)`, where `_live` is
`_probe_goal_status`. A PENDING goal takes a different branch whose retry line is a
`--phase verify` invocation carrying no suffix by design — so a test built on a pending
fixture passes before AND after breaking `_quality_flag_suffix`: green while pinning
nothing. Measured at fix time; do not "simplify" these fixtures to a synthetic id.

THE PROBE IS NOT MOCKED, AND DOES NOT NEED TO BE (measured 2026-08-28, g-115-7752).
`_probe_goal_record` is a plain `aspirations-read.sh | python3` read, not a daemon call,
so it returns "completed" identically with and without PYTEST_CURRENT_TEST set — the
g-115-3329 refusal blocks daemon SPAWNS and never reaches this path. The sibling module
test_iteration_close_recovery_probe.py exercises the *unreadable* branch because its
fixture goal id is unparseable, which is a property of that fixture and not of pytest.

FIXTURES ARE REAL, TERMINAL GOALS rather than created ones. `completed` is terminal, so
these ids cannot flip back and the module writes nothing to any store — no agent dir and
no team-state shard to clean up (the two residue surfaces the sibling module documents).
The trade is a precondition on live data, which `_require_completed` asserts loudly
rather than skipping silently: a fixture that stopped being completed must fail the run,
because a silent skip here would restore exactly the green-but-empty state this pins.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _runtime_bash import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "iteration-close.sh"
REPO = Path(__file__).resolve().parents[3]

# Three completed fixture goals ( verification (d)).
FIXTURES = ["g-115-3502", "g-115-4954", "g-115-4229"]
FLAGS = ["--tree-updated", "--artifacts-count", "3", "--encoding-score", "0.9",
         "--findings-count", "2"]
SUFFIX = " --tree-updated --artifacts-count 3 --encoding-score 0.9 --findings-count 2"


def _recovery(goal_id, *extra):
    """Fire the trap via the rb-6353 cheap route and return the RECOVERY stderr.

    `--phase state-update --goal <id>` with no --source fails the entry check at rc=2
    AFTER _CURRENT_PHASE is set, so the trap fires having executed nothing. Sub-second,
    mutates nothing.
    """
    p = subprocess.run([BASH, SCRIPT.as_posix(), "--phase", "state-update",
                        "--goal", goal_id, *extra],
                       capture_output=True, text=True, cwd=REPO.as_posix())
    return p.stderr


def _retry_line(stderr):
    for line in stderr.splitlines():
        if line.strip().startswith("Retry:"):
            return line
    return ""


def _require_completed(stderr, goal_id):
    assert "status=completed" in stderr, (
        f"fixture {goal_id} is no longer completed — this test pins the completed branch "
        f"of _print_recovery_instructions and is green-but-empty on any other status. "
        f"Pick another terminal goal id. stderr:\n{stderr}"
    )


@pytest.mark.parametrize("goal_id", FIXTURES)
def test_quality_flags_are_carried_into_the_retry_line(goal_id):
    """(a) first half — flags passed in must appear on the retry line."""
    stderr = _recovery(goal_id, *FLAGS)
    _require_completed(stderr, goal_id)
    retry = _retry_line(stderr)
    assert retry, f"no Retry: line emitted\n{stderr}"
    assert retry.endswith(SUFFIX), (
        f"retry line did not carry this call's quality flags (g-115-3480).\n"
        f"expected suffix: {SUFFIX!r}\ngot: {retry!r}"
    )


@pytest.mark.parametrize("goal_id", FIXTURES)
def test_retry_line_is_unchanged_when_no_flags_were_passed(goal_id):
    """(a) second half — the assertion that catches an OVER-EAGER suffix.

    A suffix builder that emitted defaults instead of empty would pass the test above
    and silently rewrite every flagless caller's retry line. This is the half that
    fails in that case.
    """
    stderr = _recovery(goal_id)
    _require_completed(stderr, goal_id)
    retry = _retry_line(stderr)
    assert retry.endswith("--outcome deep"), (
        f"a caller who passed NO quality flags must get the pre-fix retry line, "
        f"ending at --outcome; got: {retry!r}"
    )
    for flag in ("--tree-updated", "--artifacts-count", "--encoding-score",
                 "--findings-count"):
        assert flag not in retry, (
            f"over-eager suffix: {flag} appeared on a retry line for a call that "
            f"passed no quality flags. got: {retry!r}"
        )


@pytest.mark.parametrize("goal_id", FIXTURES)
def test_advisory_names_its_recovery_invocation_and_safety_precondition(goal_id):
    """(b) the advisory must name WHAT to run and WHY it is safe — not merely print.

    Asserting "some advisory printed" would survive the block degrading to a bare
    "state-update failed". The two load-bearing halves are the runnable retry command
    and the statement of what the completed status implies, which is what stops a
    reader reverting a closed goal.
    """
    stderr = _recovery(goal_id, *FLAGS)
    _require_completed(stderr, goal_id)
    assert f"RECOVERY (rc=2, phase=state-update, goal={goal_id})" in stderr, (
        f"recovery header must name rc, phase and goal\n{stderr}"
    )
    retry = _retry_line(stderr)
    assert "iteration-close.sh --phase state-update" in retry, (
        f"the advisory must name a runnable recovery invocation; got {retry!r}"
    )
    assert "verify succeeded" in stderr, (
        "the advisory must state the safety precondition it inferred from the live "
        f"record (status=completed => verify succeeded), so a reader does not revert a "
        f"closed goal.\n{stderr}"
    )
