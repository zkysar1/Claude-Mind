"""guard-1224 regression: bare `shift 2` in an argv re-dispatch loop hangs forever.

MECHANISM (verified empirically 2026-07-30, g-115-4003). `shift 2` with $#==1
returns non-zero and does NOT shift. Inside a `while [ $# -gt 0 ]` re-dispatch
loop that means $1 is re-processed forever. Whether that HANGS is a CONJUNCTION
of two conditions, not one:

    arm shape          set -e?   result
    ${2:-default}      no        rc=124  HANG          <-- the defect
    bare "$2"          no        rc=1    set -u catches unbound $2 first
    either             yes       rc=1    the failing shift aborts the script

Both halves matter. Testing only for `set -e` over-reports (it flags every bare
`"$2"` arm, which `set -u` already protects); testing only for the default
substitution under-reports nothing but flags the 18 files whose safety rests on
`set -e` alone. This test encodes the conjunction, so it stays quiet on the safe
shapes and loud on the real one.

The fix is the form `core/scripts/_runtime.sh` already documents at its L84:

    shift $(( $# >= 2 ? 2 : 1 ))

This is deliberately a GENERAL invariant over every `core/scripts/*.sh` rather
than a fixed list of the 9 files repaired in g-115-4003 — a new script that
introduces the pattern is the same defect and should fail here too.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from _bash_helpers import BASH

SCRIPTS = Path(__file__).resolve().parents[1]

SET_E = re.compile(r"^\s*set\s+-[a-z]*e[a-z]*\b", re.M)
ARM_START = re.compile(r"^\s*-{1,2}[A-Za-z0-9]")
BARE_SHIFT2 = re.compile(r"\bshift\s+2\b")
DEFAULT_SUBST = re.compile(r"\$\{2:-")

# Live-probeable: no mutating side effects, and a trailing value-flag exercises
# the argv loop. Executing a mutating script to test its parser could commit,
# push, promote or close real work — those are covered by the static half only.
LIVE_PROBE = {
    "evolution-stub-pending-check.sh": "--threshold-minutes",
    "gate-d-inject.sh": "--goal-id",
    "hook-fire-audit.sh": "--stale-minutes",
    "peer-surface.sh": "--window",
    "pending-deploys-gate.sh": "--agent",
    "probe-ci.sh": "--stale-hours",
    "probe-staleness-leak.sh": "--stale-hours",
}


def _offending_arms(text: str) -> list[str]:
    """Case arms that would hang: default-substituted $2 + bare `shift 2`."""
    if SET_E.search(text):
        return []
    out = []
    for line in text.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        if not ARM_START.match(line):
            continue
        if BARE_SHIFT2.search(line) and DEFAULT_SUBST.search(line):
            out.append(line.strip())
    return out


def _shell_scripts() -> list[Path]:
    return sorted(SCRIPTS.glob("*.sh"))


def test_corpus_is_non_empty():
    # rb-245 / guard-1091: a zero-length corpus would make every assertion
    # below vacuously true, so the sweep must prove it examined something.
    assert len(_shell_scripts()) > 50


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_no_bare_shift2_argv_hang(script: Path):
    arms = _offending_arms(script.read_text(encoding="utf-8", errors="replace"))
    assert not arms, (
        f"{script.name} has {len(arms)} argv arm(s) that hang forever when the "
        f"flag is passed last: default-substituted $2 + bare `shift 2`, and no "
        f"`set -e` to abort. Use `shift $(( $# >= 2 ? 2 : 1 ))` "
        f"(see _runtime.sh:84). Offending arm(s): {arms}"
    )


@pytest.mark.parametrize("name,flag", sorted(LIVE_PROBE.items()))
def test_trailing_value_flag_terminates(name: str, flag: str):
    """Behavioural half: a trailing value-flag must terminate, not spin.

    stdin is CLOSED deliberately. probe-staleness-leak.sh reads its chain from
    `$(cat)` at L47, so an inherited terminal stdin blocks there forever and
    produces the SAME rc=124 as the argv hang — two independent causes of one
    symptom. Closing stdin isolates the parser, which is what this asserts.
    """
    script = SCRIPTS / name
    if not script.exists():
        pytest.skip(f"{name} not present")
    try:
        p = subprocess.run(
            [BASH, str(script), flag],
            capture_output=True, text=True, timeout=20,
            stdin=subprocess.DEVNULL, cwd=SCRIPTS.parents[1],
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{name} {flag} did not terminate within 20s — the argv loop is "
            f"re-processing $1 forever (guard-1224)."
        )
    # Any exit status is fine; a usage error is the correct outcome for a
    # value-flag with no value. Only non-termination is the defect.
    assert p.returncode is not None
