"""predicate._eval_command_succeeds must run POSIX commands under bash, not /bin/sh.

REGRESSION PIN for g-115-9085.

`shell=True` with no `executable=` selects `/bin/sh`. On Debian/Ubuntu that is
**dash**, where `source` is not a builtin -- so the exact shape
`.claude/rules/path-resolution.md` MANDATES for world scripts, and which
`ALLOWED_COMMAND_PREFIXES` explicitly lists, died `rc=127` having run NOTHING.
That is a silent instant false-negative: `defer-recheck` and
`precondition-defer-recheck` re-probe with this same evaluator, so a
command-gated defer on such a box could never clear -- a PERMANENT freeze that
presents as a correctly-working recheck cadence.

Filed as Windows-only; measured on cc-02 (Linux, /bin/sh -> dash) 2026-09-05 and
found to reproduce identically on every box where /bin/sh is not bash.

The test asserts the REAL failure mode (a `source`-prefixed command from the
production allowlist actually succeeding), not a contract-ideal approximation --
guard-920. Windows is deliberately excluded: guard-133 requires cmd.exe
intermediation there or the python3 shim breaks.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))

import predicate  # noqa: E402

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="POSIX-only: on Windows guard-133 requires cmd.exe intermediation"
)

# A command whose prefix is genuinely in ALLOWED_COMMAND_PREFIXES, so this
# exercises the branch production takes rather than one it never reaches.
SOURCE_FORM = 'source core/scripts/_paths.sh && bash -c "exit 0"'


def test_source_form_is_actually_allowlisted():
    """If this prefix ever leaves the allowlist the pin below is measuring nothing."""
    assert any(
        SOURCE_FORM.startswith(p) for p in predicate.ALLOWED_COMMAND_PREFIXES
    ), f"the source-form prefix is no longer allowlisted: {predicate.ALLOWED_COMMAND_PREFIXES}"


@posix_only
def test_bare_shell_true_still_fails_under_sh():
    """Positive control: proves the bug is real and this box can still reproduce it.

    Without it, a green pin cannot be distinguished from a box where /bin/sh is
    already bash and the fix is doing nothing.
    """
    if os.path.realpath("/bin/sh").endswith("bash"):
        pytest.skip("/bin/sh is bash on this box -- the dash failure cannot arise here")
    out = subprocess.run(
        SOURCE_FORM, shell=True, cwd=str(PROJECT_ROOT), capture_output=True, timeout=30
    )
    assert out.returncode != 0, (
        "expected the un-fixed shape to fail under /bin/sh; if this passes, the "
        "control is dead and the pin below proves nothing"
    )


@posix_only
def test_source_form_succeeds_through_the_real_evaluator():
    """The pin: the rule-mandated source-form must evaluate true, not rc=127."""
    res = predicate._eval_command_succeeds(
        {"id": "regression-g-115-9085", "command": SOURCE_FORM, "timeout_seconds": 30}
    )
    assert res.passed is True, (
        f"source-form command_succeeds regressed to /bin/sh: "
        f"observed={res.observed_value} reason={res.reason}"
    )
    assert res.observed_value == 0
