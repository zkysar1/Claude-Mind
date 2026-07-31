"""mutation-proof-test.sh must report HOW BROAD its sabotage was ().

A sabotage that lands at the site under test AND at N-1 other occurrences makes
the resulting RED uninformative: a predicate anchored to the site and one that
merely greps the token anywhere in the file BOTH go red, so the PASS cannot tell
them apart. Measured before the fix: a deliberately vacuous whole-file predicate
was certified PASS by the tool's primary documented sabotage mode.

This is the mirror of guard-1629 (sabotage lands at the WRONG site -> the test
correctly stays green -> false ACCUSATION against good code). Here the sabotage
lands at the right site plus others -> false CERTIFICATION of a vacuous test.
guard-1636 also passes in this scenario: the sabotage genuinely applied and the
RED is genuinely the test's. Breadth is the axis none of them measure.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _bash_helpers import BASH  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "mutation-proof-test.sh"

# The token appears TWICE: once on the definition line under test, once in an
# unrelated trailing note. That is the encounter-3 shape ( round 11),
# where a survivor was saved by an unrelated CODE site rather than by a comment.
TARGET_BODY = """\
# section A -- the DEFINITION site under test
    parsed = date.fromisoformat(raw[:10])
# ... unrelated middle ...
# section Z -- unrelated note mentioning date.fromisoformat in passing
"""

# Anchored: only satisfied by the token ON the definition line.
ANCHORED = "grep -qE '^\\s+parsed = date\\.fromisoformat\\(' target.txt\n"
# Naive/vacuous: satisfied by the token ANYWHERE in the file.
NAIVE = "grep -q 'date.fromisoformat' target.txt\n"

ALL_SITES = ["--sabotage-old", "date.fromisoformat",
             "--sabotage-new", "datetime.fromisoformat"]
ONE_SITE = ["--sabotage-sed", "0,/date\\.fromisoformat/s//datetime.fromisoformat/"]


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "target.txt").write_text(TARGET_BODY, encoding="utf-8")
    (tmp_path / "anchored.sh").write_text(ANCHORED, encoding="utf-8")
    (tmp_path / "naive.sh").write_text(NAIVE, encoding="utf-8")
    return tmp_path


def run(workdir, predicate, sabotage):
    proc = subprocess.run(
        [BASH, str(SCRIPT),
         "--target", "target.txt", "--workdir", str(workdir),
         "--test-cmd", f"bash {predicate}", *sabotage],
        capture_output=True, text=True, timeout=180,
    )
    line = proc.stdout[proc.stdout.find("{"):].strip()
    assert line, f"no JSON verdict on stdout; stderr={proc.stderr[-400:]}"
    v = json.loads(line)
    # INVARIANT, enforced on every verdict this file produces rather than in one
    # test: a null site count must never be paired with a named basis. Naming
    # "occurrences" or "changed-lines" asserts a measurement happened; pairing
    # that with null lets an unmeasured breadth read as a narrow one, which is
    # the same masquerade sabotage_sites exists to prevent. Caught by the
    # post-ship read of the sed branch's own failure path.
    if v.get("sabotage_sites") is None:
        assert v.get("sabotage_sites_basis") == "unmeasured", (
            f"null sites paired with basis={v.get('sabotage_sites_basis')!r}")
    else:
        assert v.get("sabotage_sites_basis") in ("occurrences", "changed-lines")
    return v


def test_multi_site_sabotage_reports_occurrence_count(workdir):
    """An all-occurrence sabotage must report sites=2, basis=occurrences."""
    v = run(workdir, "anchored.sh", ALL_SITES)
    assert v["verdict"] == "PASS"
    assert v["sabotage_sites"] == 2
    assert v["sabotage_sites_basis"] == "occurrences"


def test_multi_site_pass_carries_the_anchoring_caveat(workdir):
    """A >1-site PASS must SAY the RED does not prove anchoring.

    The verdict deliberately stays PASS (the proof is real evidence, just for a
    smaller proposition -- guard-1856); the caveat is what stops it reading as
    proof of anchoring.
    """
    v = run(workdir, "anchored.sh", ALL_SITES)
    assert v["verdict"] == "PASS"
    assert "CAVEAT" in v["reason"]
    assert "anchored" in v["reason"]


def test_the_false_green_is_now_legible(workdir):
    """THE REGRESSION THIS FILE EXISTS FOR.

    A vacuous whole-file predicate still PASSES under all-occurrence sabotage --
    that is inherent to the mutation being broad, not a bug in the verdict. What
    must never regress is that the report says so: sites>1 plus the caveat are
    the only things distinguishing this PASS from an anchored one.
    """
    vacuous = run(workdir, "naive.sh", ALL_SITES)
    assert vacuous["verdict"] == "PASS"          # the tool cannot discriminate...
    assert vacuous["sabotage_sites"] == 2        # ...but it reports WHY it cannot
    assert "CAVEAT" in vacuous["reason"]


def test_single_site_sabotage_reports_changed_lines_and_no_caveat(workdir):
    """A one-site sed sabotage must report sites=1 via changed-lines, no caveat."""
    v = run(workdir, "anchored.sh", ONE_SITE)
    assert v["verdict"] == "PASS"
    assert v["sabotage_sites"] == 1
    assert v["sabotage_sites_basis"] == "changed-lines"
    assert "CAVEAT" not in v["reason"]


def test_single_site_sabotage_discriminates_the_vacuous_predicate(workdir):
    """Narrowing to one site is what actually catches vacuity -- the payoff.

    Same predicate, same file: PASS under the broad mutation (test above),
    FAIL here. That contrast is the reason the caveat points at --sabotage-sed.
    """
    v = run(workdir, "naive.sh", ONE_SITE)
    assert v["verdict"] == "FAIL"
    assert "VACUOUS" in v["reason"]
    assert v["sabotage_sites"] == 1


def test_basis_is_unmeasured_when_sabotage_never_applied(workdir):
    """No mutation => sites null AND basis 'unmeasured' -- never a bare null.

    Absent-vs-zero is load-bearing here for the same reason it is for red_tests:
    a bare null could be misread as 'measured, and it was narrow'.
    """
    v = run(workdir, "anchored.sh",
            ["--sabotage-old", "NO_SUCH_TOKEN_XYZ", "--sabotage-new", "x"])
    assert v["verdict"] == "FAIL"
    assert v["sabotage_sites"] is None
    assert v["sabotage_sites_basis"] == "unmeasured"


def test_restore_leaves_target_byte_identical(workdir):
    """Breadth reporting must not weaken the guaranteed restore."""
    before = (workdir / "target.txt").read_bytes()
    run(workdir, "anchored.sh", ALL_SITES)
    assert (workdir / "target.txt").read_bytes() == before


def test_self_mutation_is_refused(tmp_path):
    """Targeting the script itself must be refused LOUDLY, not attempted.

    bash re-reads a running script by byte offset, so a self-mutation resumes at
    a stale offset. The damage is length-dependent -- measured 2026-07-31, three
    +-2-byte self-mutants returned clean verdicts while a 24-byte deletion exited
    2 with a syntax error and no JSON. An erratic row inside an N-mutation matrix
    is worse than a refusal, hence exit 2 rather than a caveat.
    """
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--target", str(SCRIPT),
         "--test-cmd", "true", "--sabotage-old", "x", "--sabotage-new", "y"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2
    assert "itself" in proc.stderr
    assert proc.stdout.strip() == ""   # no verdict -- it never ran the proof


def test_self_mutation_refusal_survives_path_normalisation(tmp_path):
    """A non-canonical spelling of the same file is still the same file."""
    weird = str(SCRIPT.parent / ".." / "scripts" / SCRIPT.name)
    proc = subprocess.run(
        [BASH, str(SCRIPT), "--target", weird,
         "--test-cmd", "true", "--sabotage-old", "x", "--sabotage-new", "y"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2
    assert "itself" in proc.stderr
