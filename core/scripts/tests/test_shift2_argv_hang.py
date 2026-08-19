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
SET_U = re.compile(r"^\s*set\s+-[a-z]*u[a-z]*\b", re.M)
ARM_START = re.compile(r"^\s*-{1,2}[A-Za-z0-9]")
BARE_SHIFT2 = re.compile(r"\bshift\s+2\b")
DEFAULT_SUBST = re.compile(r"\$\{2:-")
BARE_DOLLAR2 = re.compile(r'"\$2"')

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
    """Case arms that hang when the flag is passed last. TWO shapes.

    The second shape hid a real hang until g-115-4048. The header table's
    `bare "$2" -> set -u catches unbound $2 first` row is true only when the
    file ACTUALLY sets -u, and that condition was never checked — so a bare-$2
    arm in a file with neither -e nor -u was silently treated as safe.
    `_runtime.sh` was exactly that: rt_curl's four argv arms use bare "$2", the
    file sets neither flag, and `rt_curl GET /v1/health --query` measured
    rc=124 (hung) against rc=3 after the fix — a two-way probe on the real
    production helper every daemon wrapper calls.

    Note the asymmetry that makes this worth encoding rather than assuming:
    default-substitution DEFEATS -u (measured: `set -uo pipefail` with
    `V="${2:-}"` still hangs, rc=124), while bare $2 relies on it entirely.
    So neither shape is safe on its own — each depends on a different flag
    being present, which is the decoupling this whole test exists to catch.
    """
    if SET_E.search(text):
        return []
    has_set_u = bool(SET_U.search(text))
    out = []
    for line in text.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        if not ARM_START.match(line):
            continue
        if not BARE_SHIFT2.search(line):
            continue
        if DEFAULT_SUBST.search(line):
            out.append(line.strip())
        elif not has_set_u and BARE_DOLLAR2.search(line):
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
        f"flag is passed last: a bare `shift 2` protected by neither `set -e` "
        f"nor (for a bare \"$2\" arm) `set -u`. Use `shift $(( $# >= 2 ? 2 : 1 ))` "
        f"(see _runtime.sh:84). Offending arm(s): {arms}"
    )


# The sweep above is only as good as its predicate, and a predicate that
# silently stopped matching would leave every per-script assertion vacuously
# green — the same failure mode `test_corpus_is_non_empty` guards for the
# corpus. These pin the discriminating power directly. The first case is the
# shape that escaped the ORIGINAL predicate and hung in production (,
# _runtime.sh rt_curl: rc=124 before the fix, rc=3 after).
_LOOP = 'while [ $# -gt 0 ]; do case "$1" in\n            {arm}\nesac; done\n'
_PREDICATE_CASES = [
    ("bare-$2, no -e, no -u", True, "", '--query) query="$2"; shift 2;;'),
    ("bare-$2 protected by -u", False, "set -uo pipefail\n", '--query) query="$2"; shift 2;;'),
    ("bare-$2 protected by -e", False, "set -e\n", '--query) query="$2"; shift 2;;'),
    # Default substitution DEFEATS -u (the ${2:-} expansion is never unset), so
    # -u must NOT be read as blanket protection for a file that has it.
    ("default-subst defeats -u", True, "set -uo pipefail\n", '--q) v="${2:-}"; shift 2;;'),
    ("guarded arithmetic shift", False, "", '--query) query="$2"; shift $(( $# >= 2 ? 2 : 1 ));;'),
]


@pytest.mark.parametrize(
    "label,should_flag,preamble,arm", _PREDICATE_CASES, ids=[c[0] for c in _PREDICATE_CASES]
)
def test_predicate_discriminates(label: str, should_flag: bool, preamble: str, arm: str):
    src = "#!/usr/bin/env bash\n" + preamble + _LOOP.format(arm=arm)
    assert bool(_offending_arms(src)) is should_flag, (
        f"{label}: expected {'a flag' if should_flag else 'no flag'}, got the opposite"
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
