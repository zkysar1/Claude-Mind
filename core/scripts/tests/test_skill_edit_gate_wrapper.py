"""core/scripts/skill-edit-gate.sh — the portable entry point forge-skill Step 3.5 calls.

The wrapper exists so the one gate a forge cannot skip resolves python3 through _paths.sh
like every other core script, instead of the `py -3` launcher spelling that needs a box-level
shim. These tests pin the two exit paths the skill text documents: no sub-command runs the
gate's self-test (exit 0, "SELF-TEST: PASS"); a bare `gate` is a MALFORMED call (exit 2, not a
verdict, nothing logged). A real PASS/BLOCK verdict writes telemetry to meta/, so it is not
exercised here — skill_edit_gate.py's own self-test covers the scoring.
"""
import subprocess
from pathlib import Path

from _bash_helpers import BASH  # portable bash argv[0] (guard-580)

ROOT = Path(__file__).resolve().parents[3]
WRAPPER = ROOT / "core" / "scripts" / "skill-edit-gate.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    # .as_posix(), never str(Path): bash strips the backslashes of a str(WindowsPath) (guard-581)
    return subprocess.run(
        [BASH, WRAPPER.as_posix(), *args], capture_output=True, text=True, timeout=120
    )


def test_wrapper_runs_the_gate_self_test_when_given_no_subcommand():
    r = _run()
    assert r.returncode == 0, r.stderr
    assert "SELF-TEST: PASS" in r.stdout


def test_wrapper_passes_argv_through_a_bare_gate_is_a_malformed_call():
    r = _run("gate")
    assert r.returncode == 2, (r.stdout, r.stderr)
