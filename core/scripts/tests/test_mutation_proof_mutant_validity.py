"""mutation-proof-test.sh must refuse a mutant that does not PARSE ().

The failure mode this closes is silent self-congratulation, which is the only one
in this tool that looks like success. A mutation that leaves the target
syntactically invalid reddens the WHOLE suite before any assertion runs, so a
worthless test is credited with discriminating power and nothing looks wrong.
Measured in g-115-3485: deleting `merged = merged[:limit]` orphaned the
`if limit > 0:` above it (IndentationError) and reddened all 8 merge tests;
replacing that line with `pass` instead reddened exactly 1. BOTH runs reported
5/5 detected -- only the second run's per-mutation attribution was real.

DELIBERATELY NARROW. A broad uniform red has three causes with opposite remedies:
  * the mutant does not parse             -> this guard: refuse, nothing proven
  * the TARGET's own internal guard raised -> guard-4384: the mutation was
    faithful and the wide red is legitimate; build a COMPOUND mutation instead
  * the harness never ran the code        -> guard-2546: repair the harness
So this fires ONLY on an unparseable mutant. `test_a_parseable_mutant_still_proves`
is the positive control that pins that narrowness: without it a guard that refused
EVERY mutation would satisfy every other test in this file.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _bash_helpers import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "mutation-proof-test.sh"

# One statement in the `if` body, so deleting it orphans the header.
PY_BODY = """\
def compute(flag):
    if flag:
        return 1
    return 0
"""

SH_BODY = """\
check() {
  echo ok
}
check
"""


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "t.py").write_text(PY_BODY, encoding="utf-8")
    (tmp_path / "t.sh").write_text(SH_BODY, encoding="utf-8")
    (tmp_path / "t.txt").write_text(PY_BODY, encoding="utf-8")
    # sys.executable, not a bare `python3`: the runner's interpreter is the one
    # guaranteed to exist on every box this suite runs on.
    (tmp_path / "check.sh").write_text(
        f'"{sys.executable}" -c "'
        "ns = {}; exec(open('t.py').read(), ns); assert ns['compute'](True) == 1"
        '"\n', encoding="utf-8")
    (tmp_path / "check_sh.sh").write_text(
        'out="$(bash t.sh)"; [ "$out" = "ok" ]\n', encoding="utf-8")
    (tmp_path / "check_txt.sh").write_text("grep -q 'return 1' t.txt\n", encoding="utf-8")
    return tmp_path


def run(workdir, target, predicate, *sabotage):
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--target", target, "--workdir", str(workdir),
         "--test-cmd", f"bash {predicate}", *sabotage],
        capture_output=True, text=True, timeout=180,
    )
    line = proc.stdout[proc.stdout.find("{"):].strip()
    assert line, f"no JSON verdict on stdout; stderr={proc.stderr[-400:]}"
    return proc.returncode, json.loads(line)


def test_unparseable_python_mutant_is_refused(workdir):
    """Deleting the only statement in an `if` body orphans the header."""
    rc, v = run(workdir, "t.py", "check.sh",
                "--sabotage-old", "        return 1\n", "--sabotage-new", "")
    assert rc == 1
    assert v["verdict"] == "FAIL"
    assert "not valid Python" in v["reason"]
    # The point of the guard: it must NOT hand back a RED as if it proved
    # something. A crashed mutant reddens everything, so a reported red here
    # would be exactly the false credit this test exists to prevent.
    assert v["sabotage_red"] != "true", f"reported a red for a mutant that never ran: {v}"
    # REGRESSION PIN: the refusal path must name the validator that fired.
    # It reported "unchecked" on first implementation -- the field contradicting
    # its own reason string -- because the value was assigned only after a
    # successful check, which the refusal path never reaches.
    assert v["mutant_syntax_checked"] == "python", (
        f'refusal reported mutant_syntax_checked={v["mutant_syntax_checked"]!r}; '
        "the check is precisely what fired here")


def test_refusal_still_restores_the_target(workdir):
    """The refusal is mid-cycle, so the trap -- not the happy path -- restores.

    gap-019 names "a missed restore silently ships sabotage code" as this tool's
    key failure mode, and a NEW early-exit is exactly where that regresses.
    """
    before = (workdir / "t.py").read_text(encoding="utf-8")
    rc, v = run(workdir, "t.py", "check.sh",
                "--sabotage-old", "        return 1\n", "--sabotage-new", "")
    assert rc == 1
    assert (workdir / "t.py").read_text(encoding="utf-8") == before, (
        "refusal path left the mutant on disk -- the trap did not restore")


def test_a_parseable_mutant_still_proves(workdir):
    """POSITIVE CONTROL -- the guard must not refuse valid mutants.

    Without this, a guard that refused every mutation would satisfy every other
    assertion in this file (guard-1639: an over-matching predicate is invisible
    to coverage assertions).
    """
    rc, v = run(workdir, "t.py", "check.sh",
                "--sabotage-old", "return 1", "--sabotage-new", "return 2")
    assert rc == 0, f"a parseable mutant was not proven: {v}"
    assert v["verdict"] == "PASS"
    assert v["sabotage_red"] == "true"
    assert v["mutant_syntax_checked"] == "python"


def test_unparseable_shell_mutant_is_refused(workdir):
    rc, v = run(workdir, "t.sh", "check_sh.sh",
                "--sabotage-old", "}", "--sabotage-new", "")
    assert rc == 1
    assert v["verdict"] == "FAIL"
    assert "not valid shell" in v["reason"]
    assert v["sabotage_red"] != "true"


def test_extension_without_a_validator_reports_unchecked_not_valid(workdir):
    """"We did not look" must not render the same as "we looked and it was fine".

    Same absent-vs-empty posture red_tests (null, never []) and
    sabotage_sites_basis ("unmeasured") already take in this tool.
    """
    rc, v = run(workdir, "t.txt", "check_txt.sh",
                "--sabotage-old", "return 1", "--sabotage-new", "return 2")
    assert rc == 0
    assert v["mutant_syntax_checked"] == "unchecked"


def test_every_verdict_names_whether_the_mutant_was_checked(workdir):
    """The field is never omitted -- an absent key would read as "fine"."""
    for target, pred in (("t.py", "check.sh"), ("t.txt", "check_txt.sh")):
        _, v = run(workdir, target, pred,
                   "--sabotage-old", "return 1", "--sabotage-new", "return 2")
        assert "mutant_syntax_checked" in v, f"{target}: field missing from verdict"
        assert v["mutant_syntax_checked"] in ("python", "shell", "unchecked")


def test_shell_refusal_names_the_shell_validator(workdir):
    _, v = run(workdir, "t.sh", "check_sh.sh", "--sabotage-old", "}", "--sabotage-new", "")
    assert v["mutant_syntax_checked"] == "shell"
