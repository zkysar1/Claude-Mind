"""A capture fallback in iteration-close.sh must REPLACE a bad value, never
append a second one (g-115-11674, from g-115-11570).

The shape that broke on ZDS: `x="$(producer | python3 -c '...except: print(0)' || echo 0)"`
under `set -euo pipefail`. The producer fails, python still prints 0, pipefail
makes the pipeline fail, and the fallback APPENDS: x becomes "0\\n0", and the
next `[[ "$x" -gt 0 ]]` dies with a syntax error that blames the wrong script.

Each case lifts the capture block verbatim out of iteration-close.sh (by unique
anchor lines) and runs it with a fake producer, so the test exercises the
shipped bytes rather than a copy of them.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

CLOSE_SH = CORE_SCRIPTS / "iteration-close.sh"

# (variable, producer script name, first line marker, line marker AFTER the block,
#  good producer output, value expected from it)
CASES = {
    "decompose_count": ("decompose_count", "tree-read.sh",
                        "local decompose_count",
                        'if [[ "$decompose_count" -gt 0 ]]; then',
                        "[1, 2, 3]", "3"),
    "sa_session_count": ("sa_session_count", "aspirations-read.sh",
                         "local sa_session_count",
                         'local sa_track="$AGENT_DIR/session/aspirations-incremented-session-${sa_session_count}.txt"',
                         '{"session_count": 7}', "7"),
}


def _block(start: str, stop: str) -> str:
    lines = CLOSE_SH.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == start]
    assert len(starts) == 1, f"anchor {start!r} found {len(starts)}x — re-anchor this test"
    i = starts[0]
    stops = [j for j in range(i + 1, len(lines)) if lines[j].strip() == stop]
    assert stops, f"end anchor {stop!r} not found after {start!r}"
    return "\n".join(lines[i:stops[0]])


def _run(tmp_path: Path, case: str, producer_body: str) -> subprocess.CompletedProcess:
    var, producer, start, stop, _good, _want = CASES[case]
    fake = tmp_path / "scripts"
    fake.mkdir(exist_ok=True)
    (fake / producer).write_text("#!/usr/bin/env bash\n" + producer_body + "\n")
    harness = (
        "set -euo pipefail\n"
        f"SCRIPT_DIR='{fake}'\n"
        "f() {\n" + _block(start, stop) + f'\nprintf "%s" "${var}"\n' + "}\nf\n"
    )
    return subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_failing_producer_leaves_a_single_integer(tmp_path, case):
    r = _run(tmp_path, case, "echo 'not json'; exit 1")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "0", f"{case} captured {r.stdout!r}, expected the single value '0'"


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_silent_failing_producer_leaves_a_single_integer(tmp_path, case):
    r = _run(tmp_path, case, "exit 3")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "0", f"{case} captured {r.stdout!r}"


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_good_producer_value_still_comes_through(tmp_path, case):
    """Positive control: the fix must not turn every capture into 0."""
    good, want = CASES[case][4], CASES[case][5]
    r = _run(tmp_path, case, f"echo '{good}'")
    assert r.returncode == 0, r.stderr
    assert r.stdout == want, f"{case} captured {r.stdout!r}, expected {want!r}"
