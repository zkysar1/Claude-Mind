"""Strict-argv refusal on team-state-in-flight.sh ().

THE DEFECT. This script is SET-ONLY -- the canonical clear is
team-state-clear-in-flight.sh -- but its argv loop ended in `*) shift;;`, which
DISCARDED any unrecognized flag. So

    team-state-in-flight.sh --agent X --goal-id Y --clear

did not clear: it dropped --clear and SET in_flight, and because --title/--phase
were absent it wrote an EMPTY title and EMPTY phase over a row that had carried
both, with a NEWER claimed_at, returning rc=0. in_flight is the CROSS-AGENT claim
surface, so an operation intended to RELEASE a claim instead REFRESHED it, on the
one store where a wrong answer causes duplicate or withheld work. Measured live
2026-08-19T18:27 (echo, cc-03) during the g-335-1307 close; only a read-back
caught it.

WHY THE ASSERTIONS ARE SHAPED THIS WAY. The bug is that the script WROTE, so
`rc != 0` is not the property under test and would not have caught it -- the
pre-fix path exits 0. Three rails, and the third is what keeps the second honest:

  * rc == 2 EXACTLY, never merely non-zero (guard-2066, and _argv_strict.sh's own
    header): this script's daemon path also exits non-zero on transport failure,
    so a `rc != 0` assertion stays green with the fix reverted.
  * THE WRITE STAGE IS NEVER REACHED -- neither the daemon success line
    ("in_flight set") nor the non-reducer note ("SKIP stamp") appears. Both live
    downstream of `source _runtime.sh`, which the refusal precedes.
  * A POSITIVE CONTROL in the same file (test_valid_flags_still_reach_the_write_stage)
    asserts that a VALID invocation DOES emit "SKIP stamp". Without it the bullet
    above is satisfied equally by the guard declining and by nothing having run at
    all (guard-2536) -- the control is what proves this harness can see the write
    stage when it is reached, so its absence elsewhere is signal.

WHAT THE SEAM EXCLUDES (guard-1462). These tests drive the real script, so the
argv loop, the refusal helpers and the ordering are genuinely covered. The
DAEMON-side write is not: the refusal exits before `source _runtime.sh`, so no
rt_call is ever constructed, and no assertion here can speak to what the daemon
would have done with a malformed request. That is the point rather than a gap --
what is pinned is that the refusal is UPSTREAM of every write path, local or
remote. The reducer match->stamp branch is likewise not exercised, for the reason
test_team_state_in_flight_guard.py gives: fabricating a matching
running-session-id would make this box reducer-shaped.

FAKE_AGENT has no local agent dir, so the skip branch's body-row write is gated
off and nothing lands in the SHARED team-state. Do not swap in a resident agent
name: this file would then create real shards on every suite run.

Run: STORAGE_BACKEND=local python -m pytest \
       core/scripts/tests/test_team_state_in_flight_strict_argv.py -q
"""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))
from _runtime_bash import bash_cmd  # noqa: E402

SCRIPT = (CORE_SCRIPTS / "team-state-in-flight.sh").as_posix()
SID = "22222222-2222-4222-8222-222222222222"
FAKE_AGENT = "zz-strict-argv-test"
CLEAR_SCRIPT = "team-state-clear-in-flight.sh"


def _run(*args, env_extra=None):
    env = os.environ.copy()
    env["MIND_AGENT"] = FAKE_AGENT
    env["MIND_SID"] = SID
    env.setdefault("STORAGE_BACKEND", "local")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(bash_cmd(SCRIPT, *args), capture_output=True,
                          text=True, env=env, timeout=120)


def _reached_write_stage(r):
    """True when execution got past `source _runtime.sh` into the write stage.

    Both markers are emitted downstream of that source line: "SKIP stamp" by the
    non-reducer branch, "in_flight set" by the daemon success translator.
    """
    return "SKIP stamp" in r.stderr or "in_flight set" in r.stdout


# ── the measured defect ──────────────────────────────────────────────────────

def test_clear_flag_is_refused_with_exit_2():
    r = _run("--agent", FAKE_AGENT, "--goal-id", "PROBE-NO-WRITE", "--clear")
    assert r.returncode == 2, f"rc={r.returncode}\n{r.stderr}"
    assert "refusing" in r.stderr


def test_clear_refusal_names_the_clear_script():
    """The discoverability half: the caller wanted to RELEASE a claim, and a
    script that does exactly that exists. guard-1532 -- a refusal naming a
    remediation must have that remediation reachable from the observed state;
    test_named_clear_script_exists_and_is_executable checks the reachability."""
    r = _run("--agent", FAKE_AGENT, "--goal-id", "PROBE-NO-WRITE", "--clear")
    assert CLEAR_SCRIPT in r.stderr, r.stderr


def test_named_clear_script_exists_and_is_executable():
    p = CORE_SCRIPTS / CLEAR_SCRIPT
    assert p.is_file(), f"refusal names a script that does not exist: {p}"
    assert os.access(p, os.X_OK), f"named remediation is not executable: {p}"


def test_clear_never_reaches_the_write_stage():
    """The defect was a WRITE, not a bad exit code -- this is the assertion that
    would have caught it. Paired with the positive control below."""
    r = _run("--agent", FAKE_AGENT, "--goal-id", "PROBE-NO-WRITE", "--clear")
    assert r.returncode == 2
    assert not _reached_write_stage(r), (
        f"refusal fell through to the write stage\nstdout={r.stdout}\nstderr={r.stderr}")


def test_seeded_row_is_byte_identical_after_refusal(tmp_path):
    """Seed a row, attempt the clear, assert the bytes are untouched.

    The refusal precedes `source _runtime.sh`, so no store is opened at all --
    which is why this passes with WORLD_PATH redirected at a tmp tree. The
    redirection is what makes the assertion safe to run in a live repo rather
    than what makes it meaningful; its meaning comes from being paired with
    test_clear_never_reaches_the_write_stage.
    """
    world = tmp_path / "world"
    world.mkdir()
    row = world / "team-state.yaml"
    row.write_text(
        "agent_status:\n"
        f"  {FAKE_AGENT}:\n"
        "    in_flight:\n"
        "      goal_id: g-335-1307\n"
        "      title: a title that must survive\n"
        "      phase: 4\n",
        encoding="utf-8")
    before = hashlib.sha256(row.read_bytes()).hexdigest()
    r = _run("--agent", FAKE_AGENT, "--goal-id", "g-335-1307", "--clear",
             env_extra={"WORLD_PATH": str(world)})
    after = hashlib.sha256(row.read_bytes()).hexdigest()
    assert r.returncode == 2, r.stderr
    assert before == after, "the refused call mutated the seeded row"
    assert "a title that must survive" in row.read_text(encoding="utf-8")


# ── the general flag/positional guarantee ────────────────────────────────────

def test_unknown_flag_is_refused_with_exit_2():
    r = _run("--agent", FAKE_AGENT, "--bogus")
    assert r.returncode == 2, f"rc={r.returncode}\n{r.stderr}"
    assert "unknown option" in r.stderr
    assert not _reached_write_stage(r)


def test_bare_positional_is_refused_with_exit_2():
    r = _run("--agent", FAKE_AGENT, "stray-token")
    assert r.returncode == 2, f"rc={r.returncode}\n{r.stderr}"
    assert "stray-token" in r.stderr
    assert not _reached_write_stage(r)


# ── anti-vacuity: the control that makes the negatives above mean something ──

def test_valid_flags_still_reach_the_write_stage():
    """POSITIVE CONTROL (guard-2536). Proves the harness CAN observe the write
    stage, so its absence in the refusal tests is evidence rather than silence.
    Also the regression guard on the fix itself: tightening the argv loop must
    not refuse the five accepted flags, which is the only shape production uses
    (aspirations-claim.sh passes exactly --agent/--goal-id/--title/--phase).
    """
    r = _run("--agent", FAKE_AGENT, "--goal-id", "PROBE-NO-WRITE",
             "--title", "probe", "--phase", "4")
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"
    assert _reached_write_stage(r), (
        f"valid flags did not reach the write stage\nstderr={r.stderr}")


def test_author_flag_still_accepted_and_consumes_its_value():
    """--author is accepted and value-taking. If it were ever demoted to a bare
    flag its value would fall through to the positional arm and be REFUSED, so
    this pins the arity, not merely the name."""
    r = _run("--agent", FAKE_AGENT, "--goal-id", "PROBE-NO-WRITE",
             "--author", "somebody", "--phase", "4")
    assert r.returncode == 0, f"rc={r.returncode}\n{r.stderr}"


# ── structural: the refusal must stay upstream of every write path ───────────

def test_argv_refusal_precedes_runtime_source_and_rt_call():
    src = (CORE_SCRIPTS / "team-state-in-flight.sh").read_text(encoding="utf-8")
    strict = src.index('source "$CORE_ROOT/scripts/_argv_strict.sh"')
    runtime = src.index('source "$CORE_ROOT/scripts/_runtime.sh"')
    call = src.index("rt_call POST /v1/team-state/in-flight --query")
    assert strict < runtime < call, (
        "the strict-argv refusal must be sourced and run before _runtime.sh and "
        "the POST, or a refusal could fire after a write")


def test_no_bare_catch_all_shift_arm_remains():
    """The arm that caused the defect. Compared line-wise against CODE only:
    the file's own explanatory comment quotes `*) shift;;` verbatim, and a naive
    substring check matches that prose instead of the arm (that exact mistake
    fired while this fix was being verified -- the sibling pin in
    test_body_keyed_worker_visibility.py documents the same trap from the
    positive side)."""
    src = (CORE_SCRIPTS / "team-state-in-flight.sh").read_text(encoding="utf-8")
    offenders = [ln for ln in src.splitlines() if ln.strip() == "*) shift;;"]
    assert not offenders, f"catch-all shift arm is back: {offenders}"
