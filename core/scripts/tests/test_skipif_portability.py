"""A POSIX-only call in a skipif predicate voids the whole chunk on Windows.

REGRESSION GUARD for the 2026-09-22 void run (g-115-10621, guard-7328).

`@pytest.mark.skipif(os.geteuid() != 0, ...)` looks like it scopes one test.
It does not: a skipif PREDICATE IS EVALUATED AT MODULE SCOPE, so on a box
without `os.geteuid` the bare call raises AttributeError during COLLECTION,
pytest reports rc=2 / Interrupted for the ENTIRE CHUNK, and every remaining
file in that chunk never runs. One line in test_owncloud_endpoint_flip.py
took 772 of 1545 files with it and the run emitted `VERDICT: INVALID` after
1h53m.

Correct form -- hasattr FIRST, so the short-circuit keeps the second operand
from ever being evaluated:

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, ...)

This test lives IN the suite deliberately: the population it guards is the
suite, and a file that breaks the rule is found wherever it sits, regardless
of which chunk dies. Distinct from guard-6825, which silently zeroes ONE
file's tests and reports GREEN -- this failure is loud but arrives hours late.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CHECKER = REPO / "core" / "scripts" / "check-skipif-portability.py"


def _run(target):
    return subprocess.run(
        [sys.executable, str(CHECKER), str(target)],
        capture_output=True, text=True, cwd=str(REPO),
    )


def test_live_tree_has_no_unguarded_skipif():
    """The real suite must be clean. A red here names the offending file."""
    proc = _run(REPO / "core" / "scripts" / "tests")
    assert proc.returncode == 0, (
        "unguarded POSIX-only call in a skipif predicate -- this voids the "
        "whole chunk on Windows (guard-7328):\n" + proc.stdout + proc.stderr
    )
    assert "PASS" in proc.stdout


def test_checker_catches_the_defect(tmp_path):
    """Positive control (guard-1311): the pre-fix shape MUST go red.

    Without this, a checker that silently matched nothing would pass forever
    and the guard would be indistinguishable from no guard at all.
    """
    (tmp_path / "test_fixture.py").write_text(
        "import os\nimport pytest\n\n"
        '@pytest.mark.skipif(os.geteuid() != 0, reason="pre-fix shape")\n'
        "def test_x():\n    assert True\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 1, "checker failed to flag the defect: " + proc.stdout
    assert "FAIL" in proc.stdout


def test_checker_catches_multiline_decorator(tmp_path):
    """The defect spanning lines -- what a single-line grep would miss."""
    (tmp_path / "test_fixture.py").write_text(
        "import os\nimport pytest\n\n"
        "@pytest.mark.skipif(\n"
        "    os.geteuid() != 0,\n"
        '    reason="spans lines",\n'
        ")\n"
        "def test_x():\n    assert True\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 1, "multi-line decorator not flagged: " + proc.stdout


def test_checker_accepts_the_hasattr_guard(tmp_path):
    """Negative control: the sanctioned form must NOT be flagged.

    Pins that the hasattr guard is what flips the verdict -- otherwise the
    checker could be failing for some unrelated reason.
    """
    (tmp_path / "test_fixture.py").write_text(
        "import os\nimport pytest\n\n"
        "@pytest.mark.skipif(not hasattr(os, \"geteuid\") or os.geteuid() != 0,\n"
        '                    reason="fixed shape")\n'
        "def test_x():\n    assert True\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 0, "hasattr-guarded form wrongly flagged: " + proc.stdout


def test_absent_directory_skips_rather_than_fails(tmp_path):
    """A missing target is not a regression -- must not go permanently red."""
    proc = _run(tmp_path / "does-not-exist")
    assert proc.returncode == 0
    assert "SKIP" in proc.stdout
