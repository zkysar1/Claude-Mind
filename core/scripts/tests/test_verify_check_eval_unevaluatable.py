""" — an UNEVALUATABLE check must not be reported as a FAILING one.

predicate.evaluate returns passed=False both for a check that ran and found the
work undone, and for a check the evaluator could not run at all. Those were
byte-indistinguishable in verify-check-eval, so both produced flags=
["checks_failed"], which aspirations-verify routes to "goal fails verification;
mark pending" — sending a goal that genuinely succeeded back to pending because
its check used a type name predicate.py does not implement.

THE FLAG CONTRACT HAD NO TEST COVERAGE AT ALL before this file: grep for
`checks_failed` across core/scripts/tests + core/tests returned 0 files against
851 present (positive control: `predicate` returns 151, `verify-check-eval` 2).
So these cases pin the contract as much as the fix.

Every case drives the REAL CLI through subprocess rather than importing the
functions, because the contract downstream consumers actually read is the JSON
document and the exit code — an in-process call would pass while a broken
argparse or a changed exit rule shipped (guard-920: replicate the production
call shape).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def run_checks(checks, extra=("--all",)):
    """Invoke the real CLI on a raw check array. Returns (rc, parsed_json)."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "verify-check-eval.py"),
         "--checks", json.dumps(checks), *extra],
        capture_output=True, text=True, cwd=str(SCRIPTS.parents[1]),
    )
    assert proc.stdout.strip(), f"no stdout; stderr={proc.stderr[:400]}"
    return proc.returncode, json.loads(proc.stdout)


# A check the evaluator cannot run: `code_check` is not in PREDICATE_TYPES and
# is not aliased to anything that is. 17 of these are on the live world queue.
UNEVALUATABLE = {"type": "code_check", "target": "x"}
# A check that RUNS and legitimately reports the work is not done.
GENUINE_FAIL = {"type": "file_check", "path": "no-such-file-zzz-g1154849"}
PASSING = {"type": "file_check", "path": "CLAUDE.md"}


def test_unevaluatable_alone_is_not_a_failure():
    rc, d = run_checks([UNEVALUATABLE])
    assert d["flags"] == ["checks_unevaluatable"]
    # None, not False: nothing failed. None, not True: nothing was verified.
    assert d["all_passed"] is None
    # Exit 0 matches the checks_empty fall-through — "cannot verify" is not
    # "failed". A caller branching on rc must not see this as a failure.
    assert rc == 0


def test_genuine_failure_still_fails():
    """The half that must NOT change. Without this, a fix that routed every
    failure to the fall-through would pass the test above and silently disarm
    verification entirely."""
    rc, d = run_checks([GENUINE_FAIL])
    assert d["flags"] == ["checks_failed"]
    assert d["all_passed"] is False
    assert rc == 1
    assert d["unevaluatable_count"] == 0


def test_genuine_failure_outranks_unevaluatable():
    """The safety property: an unevaluatable check must never launder a real
    failure into a fall-through. This is the one way the fix could be worse
    than the defect it replaces."""
    rc, d = run_checks([UNEVALUATABLE, GENUINE_FAIL])
    assert d["flags"] == ["checks_failed"]
    assert d["all_passed"] is False
    assert rc == 1
    # ...and the unevaluatable tally survives the branch that cannot carry it
    # in `flags`, so a consumer is not forced to infer it.
    assert d["unevaluatable_count"] == 1


def test_order_does_not_change_the_verdict():
    """fail_fast/ordering guard: the genuine failure must win regardless of
    which check the evaluator reaches first."""
    rc_a, a = run_checks([UNEVALUATABLE, GENUINE_FAIL])
    rc_b, b = run_checks([GENUINE_FAIL, UNEVALUATABLE])
    assert (rc_a, a["flags"]) == (rc_b, b["flags"]) == (1, ["checks_failed"])


def test_passing_plus_unevaluatable_does_not_report_a_pass():
    """A partially-verified set must not read as verified — that would be the
    inverse defect, and a worse one."""
    rc, d = run_checks([PASSING, UNEVALUATABLE])
    assert d["all_passed"] is None
    assert d["flags"] == ["checks_unevaluatable"]
    assert rc == 0


def test_all_passing_is_unchanged():
    rc, d = run_checks([PASSING])
    assert d["all_passed"] is True
    assert d["flags"] == []
    assert rc == 0


def test_aliased_type_is_evaluated_not_flagged():
    """'s read-time aliases must still dispatch. `command_check` is
    the single most common type on the queue and is aliased to
    command_succeeds — if this file's discriminator swallowed aliased types the
    fix would hide the very checks that DO work."""
    rc, d = run_checks([{"type": "command_check",
                         "target": "bash core/scripts/session-state-get.sh"}])
    assert d["unevaluatable_count"] == 0
    assert d["flags"] == []
    assert rc == 0


def test_fail_fast_mode_agrees_with_all_mode_on_disposition():
    """The default mode is fail_fast, not --all, and it stops at the first
    failure. An unevaluatable check first must not truncate the run into a
    reported pass."""
    rc, d = run_checks([UNEVALUATABLE, PASSING], extra=())
    assert d["all_passed"] is None
    assert d["flags"] == ["checks_unevaluatable"]
    assert rc == 0


def test_empty_contract_is_untouched():
    """checks_unevaluatable is modelled on checks_empty; assert the original
    still behaves, so the new branch cannot have absorbed it."""
    rc, d = run_checks([])
    assert d["flags"] == ["checks_empty"]
    assert d["all_passed"] is None
    assert rc == 0


def test_string_checks_flag_still_appends():
    rc, d = run_checks([UNEVALUATABLE, "a natural-language check"])
    assert d["flags"] == ["checks_unevaluatable", "has_string_checks"]
    assert d["string_checks"] == ["a natural-language check"]


@pytest.mark.parametrize("reason,expected", [
    ("unknown predicate type", True),
    ("command not in allowlist (must start with one of: ...)", True),
    ("missing required field: command", True),
    # WORK_STATE_REASONS — the evaluator RAN and reported the work is not done.
    ("found 0, need 1", False),
    ("command failed rc=1", False),
    ("timeout after 30s", False),
    ("", False),
    (None, False),
])
def test_discriminator_is_the_shared_table(reason, expected):
    """The classification is check_schema's SCHEMA_REASONS, not a local copy.

    A second copy inside verify-check-eval would drift the first time
    predicate.py grows a reason — silently, because nothing fails when a
    classification table falls behind. Importing the same helper the filing
    gate uses is what makes that drift impossible."""
    sys.path.insert(0, str(SCRIPTS))
    from gates.check_schema import is_schema_failure
    assert is_schema_failure(reason) is expected
