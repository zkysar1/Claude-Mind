"""mutation-proof-test.sh must say WHICH attribution state a null red_tests is in ().

`red_tests` and `red_count` are both null in two different situations -- nobody
asked (no --junit-xml) and the extraction failed -- and a bare null renders them
identically. The first is the DEFAULT state of the tool: --junit-xml is passed at
~4% of in-repo call sites, so ~96% of PASS verdicts carry a null that reads like a
clean attribution and is not one. guard-963 is the general form: never let
"verified nothing" render as a clean result.

WHAT IS DELIBERATELY *NOT* DONE HERE, because it is the obvious fix and it is
unsafe: defaulting --junit-xml on. That flag names where the script LOOKS for XML
the test command already wrote -- it does not tell the runner where to WRITE. A
default path nothing writes reads tests=0 and takes the hard-FAIL branch, turning
every currently-passing caller into the g-335-332 false-FAIL that Step 3b's own
comment calls worse than a missing check. So the state is NAMED rather than forced.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _bash_helpers import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "mutation-proof-test.sh"

TARGET_BODY = "    parsed = date.fromisoformat(raw[:10])\n"

# A predicate that ALSO emits junit XML, because that is the real-world shape:
# the runner writes the report and --junit-xml only points at it.
CHECK = """\
if grep -qE '^[[:space:]]+parsed = date\\.fromisoformat\\(' target.txt; then
  printf '%s' '<testsuite name="s" tests="1" failures="0"><testcase classname="c" name="the_check"/></testsuite>' > results.xml
  exit 0
else
  printf '%s' '<testsuite name="s" tests="1" failures="1"><testcase classname="c" name="the_check"><failure message="token missing">boom</failure></testcase></testsuite>' > results.xml
  exit 1
fi
"""

SABOTAGE = ["--sabotage-old", "date.fromisoformat",
            "--sabotage-new", "datetime.fromisoformat"]


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "target.txt").write_text(TARGET_BODY, encoding="utf-8")
    (tmp_path / "check.sh").write_text(CHECK, encoding="utf-8")
    return tmp_path


def run(workdir, junit=None):
    args = [BASH, str(SCRIPT),
            "--target", "target.txt", "--workdir", str(workdir),
            "--test-cmd", "bash check.sh", *SABOTAGE]
    if junit:
        args += ["--junit-xml", junit]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
    line = proc.stdout[proc.stdout.find("{"):].strip()
    assert line, f"no JSON verdict on stdout; stderr={proc.stderr[-400:]}"
    return json.loads(line)


def test_without_the_flag_attribution_is_named_unmeasured_not_left_null(workdir):
    v = run(workdir)
    assert v["verdict"] == "PASS"
    assert v["attribution"] == "unmeasured"
    # The nulls are still null -- this change NAMES the state, it does not alter
    # what red_tests means. Guarding that explicitly because the goal's own scope
    # note forbids changing the measured meaning.
    assert v["red_tests"] is None
    assert v["red_count"] is None


def test_the_unmeasured_pass_says_so_in_the_reason(workdir):
    """A field a reader never looks at is not a warning. The PASS must carry it."""
    v = run(workdir)
    assert "kill attribution was NOT measured" in v["reason"]
    assert "--junit-xml" in v["reason"]


def test_with_the_flag_attribution_is_measured_and_names_the_killer(workdir):
    v = run(workdir, junit="results.xml")
    assert v["verdict"] == "PASS"
    assert v["attribution"] == "measured"
    assert v["red_count"] == 1
    # The extractor emits the FULLY-QUALIFIED classname.name, not the bare name.
    # Asserted as production emits it rather than as I first guessed (guard-920:
    # pin the literal production shape, never the contract-ideal one).
    assert v["red_tests"] and v["red_tests"][0]["name"] == "c.the_check"


def test_the_two_states_do_not_collapse_to_one_answer(workdir):
    """ANTI-VACUITY. Identical target, identical sabotage, identical predicate --
    the ONLY difference is whether --junit-xml was passed. If both runs answered
    the same, every assertion above would pass while the field measured nothing.
    """
    without = run(workdir)
    with_flag = run(workdir, junit="results.xml")
    assert without["attribution"] != with_flag["attribution"]
    assert (without["red_count"] is None) and isinstance(with_flag["red_count"], int)


def test_the_measured_pass_does_not_carry_the_unmeasured_caveat(workdir):
    """The caveat must be attached to the STATE, not printed unconditionally.

    Without this, a caveat emitted on every PASS would satisfy the reason-text
    test above while telling the reader nothing -- the failure mode the caveat
    exists to prevent, one level up.
    """
    v = run(workdir, junit="results.xml")
    assert "kill attribution was NOT measured" not in v["reason"]
