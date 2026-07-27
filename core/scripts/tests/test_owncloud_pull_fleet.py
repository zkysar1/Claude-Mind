"""test_owncloud_pull_fleet.py — regression for  F1 + F4.

owncloud-pull.sh `--all-agents` is the FRESHNESS half of the g-115-3074 fleet
fix (pending-questions-read.sh `--all-agents` is the read half). The wrapper's
fleet path had no test coverage at all, which is how both defects below shipped:

  F1  `_fleet_roster` invoked the python launcher QUOTED. On Windows
      rt_python_launcher returns the TWO-WORD string `py -3`, so a quoted call
      looks for a single command literally named "py -3" and dies "command not
      found". The roster silently degraded to the on-disk glob fallback on
      every Windows box — the platform most of the fleet runs on.

  F4  A zero-length roster printed "0 agent(s) pulled, 0 failed" and exited 0.
      That reads as a clean sweep while NOTHING was pulled — precisely the
      silent fleet-blindness g-115-3074 exists to prevent.

Both tests drive the SHIPPED code (extracted off disk at test time), not a
copy, so they cannot drift from what actually runs. Extraction is anchored on
stable content markers rather than line numbers.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from _bash_helpers import BASH  # : never a bare "bash" argv[0]

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PULL_SCRIPT = CORE_SCRIPTS / "owncloud-pull.sh"

# PATH is inherited, NOT hand-built (). It was
# "/usr/bin:/bin:/usr/local/bin", which breaks these tests on Git Bash: `env`
# resolves there (/usr/bin/env exists) but `python3` DOES NOT, so the F1
# two-word launcher died rc=127 "env: 'python3': No such file or directory",
# _fleet_roster fell back to on-disk agent dirs, and the assertion read as a
# roster-parsing bug. Measured 2026-07-26 — POSIX-only PATH: `command -v python3`
# rc=1; ambient PATH: `env python3 -c 'print(42)'` -> 42.
#
# This does NOT weaken what F1 tests. The invariant is that the launcher WORD-SPLITS
# (two words, unquoted), and `env python3` is still two words — a faithful stand-in
# for Windows `py -3`. Only its resolvability changed. Hermeticity is unaffected:
# it comes from the stubbed CORE_ROOT/scripts/team-state-read.sh, not from PATH.
BASE_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")}


def _extract(pattern: str) -> str:
    """Pull a block out of the shipped wrapper by content anchor."""
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    m = re.search(pattern, src, re.M | re.S)
    assert m, f"anchor not found in owncloud-pull.sh: {pattern!r}"
    return m.group(0)


# --- F1: two-word launcher (Windows `py -3`) ---------------------------------

def test_fleet_roster_parses_with_a_two_word_launcher():
    """The roster must parse when the launcher is two words, not "command not found".

    `env python3` is a faithful Linux stand-in for Windows `py -3`: both are a
    single string that must word-split into a command plus an argument. A
    quoted "$PYLAUNCH" cannot word-split, so it fails identically on both.
    """
    roster_fn = _extract(r"^_fleet_roster\(\) \{\n.*?^\}$")

    with tempfile.TemporaryDirectory() as tmpd:
        core_root = Path(tmpd)
        (core_root / "scripts").mkdir()
        # Stub the daemon-backed roster read — hermetic, no live team-state.
        ts = core_root / "scripts" / "team-state-read.sh"
        ts.write_text(
            '#!/usr/bin/env bash\n'
            'echo \'{"agent_status":{"alpha":{},"zeta":{}}}\'\n',
            encoding="utf-8",
        )
        harness = core_root / "harness.sh"
        harness.write_text(roster_fn + "\n_fleet_roster\n", encoding="utf-8")

        r = subprocess.run(
            [BASH, str(harness)],
            env={**BASE_ENV, "PYLAUNCH": "env python3",
                 "CORE_ROOT": str(core_root)},
            capture_output=True, text=True,
        )

    assert "command not found" not in r.stderr, (
        f"two-word launcher must word-split, not be treated as one command: {r.stderr!r}"
    )
    assert r.stdout.split() == ["alpha", "zeta"], (
        f"roster must parse from team-state, got {r.stdout!r} / {r.stderr!r}"
    )
    assert "falling back to on-disk agent dirs" not in r.stderr, (
        "a working team-state read must NOT degrade to the glob fallback"
    )


def test_pylaunch_is_never_quoted_in_command_position():
    """Guards the other call sites against re-acquiring the same quoting bug.

    A quoted "$PYLAUNCH" is CORRECT inside a `[ -n ... ]` presence test and
    WRONG in command position — the distinction the fix turns on. Flag any
    quoted occurrence that is not a presence test.
    """
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    # Both spellings — "$PYLAUNCH" and "${PYLAUNCH}" — word-split identically
    # (i.e. not at all) when quoted, so both are bugs in command position. The
    # brace form was a false negative in the first cut of this check.
    quoted = re.compile(r'"\$\{?PYLAUNCH\}?"')
    presence_test = re.compile(r'-[nz] +"\$\{?PYLAUNCH\}?"')
    offenders = [
        (n, line) for n, line in enumerate(src.splitlines(), 1)
        if quoted.search(line) and not presence_test.search(line)
    ]
    assert not offenders, (
        "\"$PYLAUNCH\" in command position cannot word-split the Windows "
        f"two-word launcher `py -3`: {offenders}"
    )


# --- F4: zero roster is a failure, not a clean sweep -------------------------

def _run_fleet_block(roster_stub: str):
    """Drive the shipped fleet block with a stubbed roster + pull."""
    block = _extract(r'^if \[ -n "\$ALL_AGENTS" \]; then\n.*?^fi$')
    harness = (
        'ALL_AGENTS=1\n'
        'PYLAUNCH=""\n'
        'RESPONSE=\'{"ok":true,"agent":"x","pulled":1}\'\n'
        '_pull_one_agent() { return 0; }\n'
        'rt_no_daemon_error() { echo "no-daemon" >&2; exit 3; }\n'
        f'_fleet_roster() {{ {roster_stub}; }}\n'
    ) + block + "\n"
    with tempfile.TemporaryDirectory() as tmpd:
        h = Path(tmpd) / "h.sh"
        h.write_text(harness, encoding="utf-8")
        return subprocess.run([BASH, str(h)], env=dict(BASE_ENV),
                              capture_output=True, text=True)


def test_zero_roster_fails_loud_instead_of_reporting_a_clean_sweep():
    """Both roster sources failing must be a nonzero, diagnosed exit."""
    r = _run_fleet_block(":")
    assert r.returncode != 0, (
        "an empty roster pulled NOTHING — exiting 0 reports silent success"
    )
    assert "ZERO agents" in r.stderr
    assert "0 agent(s) pulled" not in r.stdout, (
        "the false-success summary must not be printed on a zero roster"
    )


def test_populated_roster_still_succeeds():
    """The F4 guard must not fire on the normal path (no false positive)."""
    r = _run_fleet_block('printf "alpha\\nzeta\\n"')
    assert r.returncode == 0, f"normal sweep must pass: {r.stderr!r}"
    assert "2 agent(s) pulled, 0 failed" in r.stdout


if __name__ == "__main__":
    test_fleet_roster_parses_with_a_two_word_launcher()
    test_pylaunch_is_never_quoted_in_command_position()
    test_zero_roster_fails_loud_instead_of_reporting_a_clean_sweep()
    test_populated_roster_still_succeeds()
    print("ok")
