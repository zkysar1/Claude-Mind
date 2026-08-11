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

def _run_fleet_block(roster_stub: str, pull_stub: str = "return 0"):
    """Drive the shipped fleet block with a stubbed roster + pull.

    `pull_stub` is the BODY of `_pull_one_agent`, so a test can make one roster
    entry fail with a chosen rc while the rest succeed. It receives the agent
    name as $1, exactly as the real function does.

    A stub may append "$1" to "$TOUCHED" to record which agents were actually
    ATTEMPTED; the names land on the returned object as `.attempted`. That is
    the only direct evidence for "the sweep aborted and did NOT try the rest" —
    asserting on the summary line proves only that the summary never printed,
    which a crash would satisfy too.

    TOUCHED is exported by the harness on purpose. It was ONCE used by stubs
    without being defined anywhere (caught by fresh-eyes 2026-08-08, g-115-4580):
    bash expanded it to empty, `>> ""` died "No such file or directory" on every
    call, and the tests still passed — because rc propagation carried them and
    nothing asserted on the file. A dead write that leaves the suite green is
    worse than no write at all, since it reads as attempt-tracking that is not
    happening.
    """
    block = _extract(r'^if \[ -n "\$ALL_AGENTS" \]; then\n.*?^fi$')
    with tempfile.TemporaryDirectory() as tmpd:
        touched = Path(tmpd) / "attempted"
        harness = (
            'ALL_AGENTS=1\n'
            'PYLAUNCH=""\n'
            'RESPONSE=\'{"ok":true,"agent":"x","pulled":1}\'\n'
            f'TOUCHED="{touched.as_posix()}"\n'
            f'_pull_one_agent() {{ {pull_stub}; }}\n'
            'rt_no_daemon_error() { echo "no-daemon" >&2; exit 3; }\n'
            f'_fleet_roster() {{ {roster_stub}; }}\n'
        ) + block + "\n"
        h = Path(tmpd) / "h.sh"
        h.write_text(harness, encoding="utf-8")
        proc = subprocess.run([BASH, str(h)], env=dict(BASE_ENV),
                              capture_output=True, text=True)
        # Read INSIDE the context manager — the tempdir is gone after it exits.
        proc.attempted = ([l for l in touched.read_text(encoding="utf-8").splitlines() if l]
                          if touched.exists() else [])
        return proc


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


# --- F6: one slow agent must not abort the sweep () ----------------
# The file's own header promises "Per-agent failures are ISOLATED — one
# unreachable agent must not abort the sweep". The fleet loop broke that promise
# for exactly one rc: `3) rt_no_daemon_error` EXITS, so a single peer taking
# longer than RT_CURL_TIMEOUT killed every agent after it. Roster order is
# sorted, so it was always the same tail that silently went unrefreshed — the
# fleet blindness  exists to prevent, reintroduced by the fix for it.
#
# Both tests below are needed and neither is redundant: the first proves
# isolation happens, the second proves it was NOT achieved by simply deleting
# the abort. A fix that isolated everything would pass the first alone while
# turning a dead daemon into N identical failures with the one useful message
# buried.
#
# WHAT THESE TESTS DO AND DO NOT PIN — measured, not assumed (mutation controls
# run 2026-08-08, cc-03, Linux 6.8.0-136-generic). Delete the `4)` branch from
# the case statement and ONLY the `TIMED OUT` assertion goes red: the generic
# `*)` branch isolates too, so `2 pulled, 1 failed` and the non-zero exit stay
# green on the mutant. So the isolation assertions here pin the CONTRACT, not
# the defect, and the diagnostic string is the one doing real work.
#
# The load-bearing regression pin for  is NOT in this file — it is
# `test_owncloud_pull_timeout_no_retry.py::test_timeout_does_not_retry`'s
# `rc != absent-daemon rc` assertion. Collapse `return 4` back to `return 3`
# in _pull_one_agent (the literal pre-fix source) and that assertion goes red
# while every test in THIS file stays green, because the fleet loop never sees
# a 4 to route. Do not read a green fleet suite as proof the split survived.

def test_a_timed_out_agent_is_isolated_and_the_roster_continues():
    """rc=4 (daemon healthy, request slow): count it, keep going, exit nonzero."""
    r = _run_fleet_block(
        'printf "alpha\\nbravo\\nzeta\\n"',
        pull_stub='echo "$1" >> "$TOUCHED"; [ "$1" = "alpha" ] && return 4; return 0',
    )
    assert r.returncode != 0, (
        "a failed agent must still make the sweep exit nonzero, or a partial "
        f"refresh reads as a clean one: {r.stdout!r} {r.stderr!r}"
    )
    assert "3 agent(s)" not in r.stdout, "the timed-out agent must not count as pulled"
    assert "2 agent(s) pulled, 1 failed" in r.stdout, (
        f"expected 2 pulled / 1 failed, got: {r.stdout!r}"
    )
    assert "TIMED OUT" in r.stderr, (
        "the isolated timeout needs its own diagnostic — reusing the generic "
        f"FAILED line loses the reason: {r.stderr!r}"
    )
    assert "no-daemon" not in r.stderr, (
        "rt_no_daemon_error fired on a REACHABLE daemon — that is the abort path"
    )
    assert r.attempted == ["alpha", "bravo", "zeta"], (
        "every roster entry must be ATTEMPTED after one times out — this is the "
        "direct evidence the sweep continued past the slow peer, and the whole "
        f"point of g-115-4580: {r.attempted!r}"
    )


def test_an_absent_daemon_still_aborts_the_whole_sweep():
    """rc=3 (nothing will succeed): abort, do NOT attempt the remainder."""
    r = _run_fleet_block(
        'printf "alpha\\nbravo\\nzeta\\n"',
        pull_stub='echo "$1" >> "$TOUCHED"; [ "$1" = "alpha" ] && return 3; return 0',
    )
    assert r.returncode != 0
    assert "no-daemon" in r.stderr, (
        "an absent daemon must reach rt_no_daemon_error, not be isolated — "
        "isolating it prints one failure per roster entry and buries the "
        f"single message that explains all of them: {r.stderr!r}"
    )
    assert "agent(s) pulled" not in r.stdout, (
        "the sweep summary must not print after an abort — it would imply the "
        f"roster completed: {r.stdout!r}"
    )
    assert r.attempted == ["alpha"], (
        "the remainder of the roster must NOT be attempted once the daemon is "
        "known absent — nothing later can succeed, so continuing would print N "
        "identical failures and bury the one message that explains them. This "
        "is the assertion the stdout check above cannot make: a crash would "
        f"also suppress the summary. attempted={r.attempted!r}"
    )


# --- F5: _do_call query construction () ----------------------------
# The flag -> query-param wiring is the ONE link in the --with-temp chain that
# neither end's tests cover: test_runtime_owncloud_flush.py pins
# query -> include_temp at the endpoint, and test_owncloud_sync.py pins
# include_temp -> behavior in the function, but nothing pinned the shell
# BUILDING that query. A typo here (`with-temp=1`, `withtemp=1`) makes the flag
# a silent no-op — the endpoint reads a param nobody sent, defaults it off, and
# every other test in the chain still passes. Same class as the `only=` param it
# sits beside, which was equally unpinned.

def _run_do_call(only: str = "", with_temp: str = "") -> str:
    """Run the shipped _do_call with rt_call/rt_url_encode stubbed, return the query."""
    do_call = _extract(r"^_do_call\(\) \{\n.*?^\}$")
    with tempfile.TemporaryDirectory() as tmpd:
        harness = Path(tmpd) / "harness.sh"
        harness.write_text(
            "rt_url_encode() { printf '%s' \"$1\"; }\n"
            "rt_call() { printf '%s' \"$4\"; }\n"   # $4 is the --query VALUE
            f"ONLY={only!r}\nWITH_TEMP={with_temp!r}\n"
            + do_call + "\n_do_call alpha\n",
            encoding="utf-8",
        )
        r = subprocess.run([BASH, str(harness)], env=dict(BASE_ENV),
                           capture_output=True, text=True)
    assert r.returncode == 0, f"_do_call harness failed: {r.stderr!r}"
    return r.stdout


def test_do_call_bare_query_is_agent_only():
    assert _run_do_call() == "agent=alpha"


def test_do_call_appends_only_when_set():
    assert _run_do_call(only="pending-questions.yaml") == \
        "agent=alpha&only=pending-questions.yaml"


def test_do_call_appends_with_temp_using_the_exact_param_name():
    """The param name must be `with_temp` — the spelling admin.py reads.

    Asserted as an exact string, not a substring: `with_temp` is the whole
    contract, and an underscore/hyphen slip is invisible everywhere else.
    """
    assert _run_do_call(with_temp="1") == "agent=alpha&with_temp=1"


def test_do_call_with_temp_absent_by_default():
    """No --with-temp -> the param is not sent at all (endpoint defaults it off)."""
    assert "with_temp" not in _run_do_call()


if __name__ == "__main__":
    test_fleet_roster_parses_with_a_two_word_launcher()
    test_pylaunch_is_never_quoted_in_command_position()
    test_zero_roster_fails_loud_instead_of_reporting_a_clean_sweep()
    test_populated_roster_still_succeeds()
    test_do_call_bare_query_is_agent_only()
    test_do_call_appends_only_when_set()
    test_do_call_appends_with_temp_using_the_exact_param_name()
    test_do_call_with_temp_absent_by_default()
    print("ok")
