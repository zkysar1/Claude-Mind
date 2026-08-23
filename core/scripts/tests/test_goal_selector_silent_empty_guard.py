#!/usr/bin/env python3
"""Regression pins for goal-selector.sh's silent-empty guard (, ).

THE DEFECT CLASS: goal-selector.sh is the MANDATORY Phase 2 selection instrument.
When it returns rc=0 with zero bytes, a caller cannot distinguish that from "the
selector ran and found no candidates" — and under the no-terminal-state doctrine
an empty candidate list AUTHORIZES a branch (B3/B4/B5 work generation, the
all-blocked handler, or a long quiescence sleep). So a silent zero does not merely
report; it acts. The wrapper's lines 28-31 convert that signature into a loud
exit 7. This file pins that conversion, which had NO test before g-115-7093 —
the sibling pin `test_retrieve_timeout_loud.py` names goal-selector.sh's exit 7 as
its model while goal-selector.sh itself went unpinned.

THE CONTRACT PINNED HERE (maps to g-115-7093's verification outcomes):
  1. python exiting 0 with EMPTY stdout exits 7 with a FATAL diagnostic naming
     g-115-6146 — outcome 2's positive control, so the guard can never go inert
     without a red test;
  2. a normal body passes through byte-identical at rc=0 (the guard must not
     fire on the healthy path);
  3. a NON-ZERO python rc propagates unchanged and is NOT masked into 7 — the
     distinct-code contract in the wrapper's comment ("distinct from python
     tracebacks (1), argparse (2), daemon-unreachable (3), timeout kills (124)")
     only holds if 7 stays reserved for the empty case;
  4. a BARE invocation (no subcommand) forwards `select` to the .py.

WHY (4) IS PINNED — it corrects two live guardrails, and the correction is the
reason this file exists. guard-3667 (zeta, cc-02, 2026-08-13) and guard-3787
(bravo, cc-05, 2026-08-14) both state that a bare `goal-selector.sh` "exits rc=0
and writes ZERO BYTES", attributing the zero-byte signature to the missing
subcommand. That cause is impossible: `CMD="${1:-select}"` has defaulted the
subcommand since 2026-03-19 (41a788ae6), five months before either measurement,
so bare has been byte-equivalent to `select` the whole time. Re-measured
2026-08-21 (echo, hostname cc-03, uname -r 6.8.0-137-generic): bare returned
rc=0 with 2,568,352 bytes in 41.9s. The raw `.py` with no subcommand is the one
that refuses, loudly and correctly — rc=2 with an argparse usage line.

Both guardrails' ADVICE (always pass the subcommand; positive-control a zero
before acting on it) is good and untouched. Only their causal claim is wrong,
and a wrong cause is worse than none here: it sends the next reader to the
invocation shape and away from the real common factor, which is that `select`
takes ~42s and emits ~2.6MB while `blocked` takes ~3.5s and emits ~519KB. Those
two differ 12x in DURATION, not only in subcommand — so "blocked worked, select
didn't" does not isolate the subcommand at all.

Hermetic and fast: a stub `python3` on PATH stands in for goal-selector.py, so
no test here runs the real 42-second scorer or touches any store.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402  (guard-580: never a bare "bash" argv)

SCRIPTS = _TESTS_DIR.parent
PROJECT_ROOT = SCRIPTS.parent.parent
WRAPPER = SCRIPTS / "goal-selector.sh"

# Resolve the REAL interpreter now, before the stub shadows the name on PATH.
_REAL_PY = os.path.realpath(sys.executable or "/usr/bin/python3")


def _stub_python(tmp_path, body: str):
    """Write a `python3` that intercepts goal-selector.py and delegates the rest.

    The wrapper calls `python3` unqualified (line 24), but _paths.sh and
    _platform.sh also invoke it while being sourced. Delegating every other
    argv to the real interpreter keeps those working, so the stub isolates the
    one call under test instead of breaking the wrapper's own setup.
    """
    binddir = tmp_path / "stubbin"
    binddir.mkdir(exist_ok=True)
    stub = binddir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *goal-selector.py)\n"
        f"{body}\n"
        "    ;;\n"
        f'  *) exec "{_REAL_PY}" "$@" ;;\n'
        "esac\n"
    )
    stub.chmod(0o755)
    return binddir


def _run(binddir, args=("select",)):
    env = os.environ.copy()
    env["PATH"] = f"{binddir}{os.pathsep}{env.get('PATH', '')}"
    env["STORAGE_BACKEND"] = "local"  # guard-955: never let a test touch own-cloud
    env["MIND_SKIP_PY_SHIM"] = "1"  # prevent _paths.sh from prepending .python-shim
    return subprocess.run(
        [BASH, WRAPPER.as_posix(), *args],
        capture_output=True, text=True, env=env,
        cwd=str(PROJECT_ROOT), timeout=120,
    )


def test_empty_stdout_at_rc0_exits_7_with_fatal(tmp_path):
    """Outcome 2: the guard FIRES on a synthetic empty — it is not inert."""
    binddir = _stub_python(tmp_path, "    exit 0")
    r = _run(binddir)
    assert r.returncode == 7, (r.returncode, r.stderr[-500:])
    assert r.stdout == "", "an empty-produce failure must not emit stdout"
    assert "FATAL" in r.stderr
    assert "g-115-6146" in r.stderr
    # The caller's whole problem is mistaking this for "no candidates", so the
    # diagnostic must say so in words, not merely exit non-zero.
    assert "no candidates" in r.stderr


def test_normal_body_passes_through_byte_identical(tmp_path):
    """The guard must not fire on the healthy path."""
    payload = '[{"goal_id":"g-1-1","score":1.5}]'
    binddir = _stub_python(tmp_path, f"    printf '%s' '{payload}'; exit 0")
    r = _run(binddir)
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert r.stdout == payload, "body must pass through byte-identical"


def test_empty_ranking_is_not_the_empty_signature(tmp_path):
    """A genuinely empty ranking prints '[]' — 2 bytes, so rc=0 and NO exit 7.

    This is the distinction the whole guard rests on (the wrapper's comment:
    "an empty ranking is '[]', never ''"). Without this pin, someone could
    'fix' a false exit-7 report by widening the guard to catch '[]' and would
    silently convert every legitimately-empty queue into a fatal error.
    """
    binddir = _stub_python(tmp_path, "    printf '[]'; exit 0")
    r = _run(binddir)
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert r.stdout == "[]"


@pytest.mark.parametrize("rc", [1, 2, 3, 124])
def test_nonzero_rc_propagates_and_is_not_masked_into_7(tmp_path, rc):
    """Exit 7 stays RESERVED for the empty case.

    The wrapper's comment promises 7 is distinct from tracebacks (1), argparse
    (2), daemon-unreachable (3) and timeout kills (124) so a caller's rc log
    alone identifies the signature. That promise breaks if any of those get
    rewritten to 7 on their way out.
    """
    binddir = _stub_python(tmp_path, f"    exit {rc}")
    r = _run(binddir)
    assert r.returncode == rc, (r.returncode, r.stderr[-500:])
    assert r.returncode != 7


def test_bare_invocation_forwards_select(tmp_path):
    """Pins the `CMD="${1:-select}"` default that falsifies guard-3667/guard-3787.

    Kept as a live assertion rather than only a prose correction: the claim
    "bare writes zero bytes" was measured twice, on two boxes, by two agents,
    and written into two active guardrails. A pin is what stops it returning a
    third time.
    """
    binddir = _stub_python(tmp_path, '    printf "ARGV:%s" "$2"; exit 0')
    r = _run(binddir, args=())
    assert r.returncode == 0, (r.returncode, r.stderr[-500:])
    assert r.stdout == "ARGV:select", (
        "bare invocation must default to the select subcommand; "
        f"got {r.stdout!r}"
    )
