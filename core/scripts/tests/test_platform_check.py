"""Unit tests for core/scripts/platform-check.sh ().

The script makes a goal's platform constraint machine-readable so
goal-selector.py can filter on it via a `command_succeeds` precondition.

WHY THE MSYSTEM TESTS BELOW ARE THE LOAD-BEARING ONES. The script deliberately
detects via `case "$(uname -s)"` rather than the `[ -n "$MSYSTEM" ]` idiom in
_platform.sh, because predicate.py fails CLOSED (any non-zero exit hides the
goal on every box) and an env var is the signal most likely to go absent across
a spawn. A future edit that "unifies" the two idioms would reintroduce exactly
that fragility while still passing every positive test -- so the env-independence
assertions are the mutation-proof for the design decision, not decoration.
"""

import os
import platform
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _bash_helpers import BASH  # noqa: E402

# SCRIPT_DIR is core/scripts/tests -> three hops to the repo root, not two.
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
SCRIPT = PROJECT_ROOT / "core" / "scripts" / "platform-check.sh"


def run(args, env=None):
    full_env = dict(os.environ)
    if env is not None:
        for k, v in env.items():
            if v is None:
                full_env.pop(k, None)
            else:
                full_env[k] = v
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
    )


def this_platform():
    """Expected verdict for the box actually running the tests."""
    s = platform.system()
    if s == "Linux":
        return "linux"
    if s == "Darwin":
        return "macos"
    if s == "Windows":
        return "windows"
    # Git Bash reports platform.system() == "Windows" via CPython even under
    # MSYS2, so this fallback is defensive only.
    return "unknown"


ALL_PLATFORMS = ("windows", "linux", "macos")


# --- core behavior -------------------------------------------------------

def test_script_exists_and_is_readable():
    assert SCRIPT.is_file(), f"{SCRIPT} missing"


def test_matching_platform_exits_zero():
    me = this_platform()
    assert me in ALL_PLATFORMS, f"test box reports unsupported platform {me!r}"
    r = run(["--os", me])
    assert r.returncode == 0, (
        f"--os {me} should exit 0 on this box "
        f"(platform.system()={platform.system()!r}); got rc={r.returncode} "
        f"stderr={r.stderr!r}"
    )


def test_non_matching_platforms_exit_one():
    me = this_platform()
    others = [p for p in ALL_PLATFORMS if p != me]
    assert others, "expected at least one non-matching platform"
    for other in others:
        r = run(["--os", other])
        assert r.returncode == 1, (
            f"--os {other} should exit 1 on a {me} box; got rc={r.returncode}"
        )


def test_case_insensitive_os_value():
    me = this_platform()
    r = run(["--os", me.upper()])
    assert r.returncode == 0, f"--os {me.upper()} should match; rc={r.returncode}"


# --- usage errors are exit 2, distinct from a 1 mismatch -----------------

def test_missing_os_flag_is_usage_error():
    r = run([])
    assert r.returncode == 2, f"no --os should exit 2; got {r.returncode}"
    assert "required" in r.stderr.lower()


def test_unknown_platform_value_is_usage_error():
    r = run(["--os", "solaris"])
    assert r.returncode == 2, f"unknown platform should exit 2; got {r.returncode}"
    assert "solaris" in r.stderr


def test_unknown_argument_is_usage_error():
    r = run(["--platform", "linux"])
    assert r.returncode == 2, f"unknown arg should exit 2; got {r.returncode}"


def test_usage_error_is_distinguishable_from_mismatch():
    """exit 2 (caller bug) must not be confused with exit 1 (real mismatch).

    Both hide the goal because predicate.py fails closed, so the exit codes are
    the ONLY way to tell a malformed precondition from a correct negative.
    """
    me = this_platform()
    other = next(p for p in ALL_PLATFORMS if p != me)
    assert run(["--os", other]).returncode == 1
    assert run(["--os", "nonsense"]).returncode == 2


# --- the design-decision mutation-proof ----------------------------------

def test_verdict_does_not_depend_on_msystem_being_set():
    """Setting MSYSTEM must not make a non-Windows box report Windows.

    If someone re-implements detection on MSYSTEM (the _platform.sh idiom),
    this fails on Linux/macOS: MSYSTEM=MINGW64 would flip the verdict.
    """
    me = this_platform()
    r = run(["--os", "windows"], env={"MSYSTEM": "MINGW64"})
    if me == "windows":
        assert r.returncode == 0
    else:
        assert r.returncode == 1, (
            "MSYSTEM=MINGW64 must not make a non-Windows box answer 'windows' — "
            "detection must key off uname -s, not an inherited env var"
        )


def test_verdict_does_not_depend_on_msystem_being_absent():
    """Scrubbing MSYSTEM must not change the answer on any box.

    On Windows this is the half that matters: an MSYSTEM-based detector would
    report 'not windows' once the variable is scrubbed by a spawn, which is
    precisely the fail-closed disappearance this script exists to avoid.
    """
    me = this_platform()
    r = run(["--os", me], env={"MSYSTEM": None})
    assert r.returncode == 0, (
        f"--os {me} must still match with MSYSTEM unset; rc={r.returncode}"
    )


def test_does_not_export_msys_no_pathconv():
    """The script must not source _platform.sh.

    _platform.sh exports MSYS_NO_PATHCONV=1, which is the ordering hazard that
    silently killed the pre-edit advisory gate (read-before-edit.md Rule 4). A
    detector that breaks its callers is worse than a duplicated case statement.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    code_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    body = "\n".join(code_lines)
    assert "_platform.sh" not in body, "platform-check.sh must not source _platform.sh"
    assert "_paths.sh" not in body, "platform-check.sh must stay dependency-free"
    assert "MSYS_NO_PATHCONV" not in body


# --- the shape the selector will actually invoke -------------------------

def test_allowlisted_command_shape_runs_from_project_root():
    """predicate.py runs `bash core/scripts/...` with cwd=PROJECT_ROOT, shell=True.

    Reproduce that literal shape (guard-920: replicate the production arg shape,
    not the contract-ideal one) so an allowlist/cwd regression is caught here.
    """
    me = this_platform()
    cmd = f"bash core/scripts/platform-check.sh --os {me}"
    assert cmd.startswith("bash core/scripts/"), "must satisfy predicate allowlist"
    r = subprocess.run(
        cmd, shell=True, cwd=str(PROJECT_ROOT), capture_output=True, timeout=60
    )
    assert r.returncode == 0, (
        f"production-shape invocation failed: rc={r.returncode} "
        f"stderr={r.stderr!r}"
    )


def test_predicate_evaluates_the_precondition_end_to_end():
    """Drive the real predicate registry, not an approximation of it."""
    sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
    from predicate import evaluate_all  # noqa: E402

    me = this_platform()
    other = next(p for p in ALL_PLATFORMS if p != me)

    passing = [{"id": "pc-match", "type": "command_succeeds",
                "command": f"bash core/scripts/platform-check.sh --os {me}"}]
    failing = [{"id": "pc-miss", "type": "command_succeeds",
                "command": f"bash core/scripts/platform-check.sh --os {other}"}]

    got_pass = evaluate_all(passing, mode="fail_fast", include_skippable=False)
    assert got_pass, "evaluate_all returned no results for the matching predicate"
    assert all(r.passed for r in got_pass)

    got_fail = evaluate_all(failing, mode="fail_fast", include_skippable=False)
    assert got_fail, "evaluate_all returned no results for the mismatching predicate"
    assert any(not r.passed for r in got_fail), (
        "a mismatched platform must FAIL the predicate — this is the direction "
        "that actually hides the goal from the selector"
    )


# --- malformed-input regressions (found by /fresh-eyes-code on this file) ----
#
# Both of these were LIVE DEFECTS in the first shipped version, and both were
# missed by the 13 tests above because those covered a BAD --os value and a
# MISSING --os flag, but never a --os flag present with its value absent, and
# never a failing uname. Pre-fix measurements, taken on the real script:
#   valueless --os  -> rc=124 under `timeout 5` (infinite loop)
#   broken uname    -> rc=1 for `--os linux` ON A LINUX BOX (false mismatch)
# Those two numbers are the guard-1475 proof that these tests can fail.

def test_valueless_os_flag_exits_two_instead_of_hanging():
    """`--os` as the final argument must not spin forever (guard-1224).

    Bare `shift 2` with $#==1 is out-of-range: bash does not shift, so the
    parse loop re-processes the same $1 forever, BEFORE any validation. The
    consumer is predicate.py, which would burn its 30s timeout on every gated
    goal on every selector pass and then fail closed with no diagnostic — so a
    single typo'd precondition degrades selection fleet-wide and silently.
    """
    try:
        r = subprocess.run(
            [BASH, str(SCRIPT), "--os"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "platform-check.sh --os HUNG — the arg parser is re-processing $1 "
            "forever. Use `shift $(( $# >= 2 ? 2 : 1 ))`, never bare `shift 2` "
            "(guard-1224)."
        )
    assert r.returncode == 2, (
        f"valueless --os should be a usage error (2); got {r.returncode}"
    )


def test_detection_failure_is_not_reported_as_a_platform_mismatch(tmp_path):
    """A broken `uname` must exit 2 (cannot tell), never 1 (does not match).

    predicate.py fails closed on ANY non-zero, so both codes hide the goal —
    the difference is whether a human can tell WHY. Collapsing them means a box
    with a broken uname hides every gated goal while looking exactly like an
    ordinary wrong-platform result.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    fake = shim / "uname"
    fake.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
    fake.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"

    me = this_platform()
    r = subprocess.run(
        [BASH, str(SCRIPT), "--os", me],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=env, timeout=30,
    )

    # Control: confirm the shim actually took effect. If the platform resolves
    # `uname` some other way (a shell builtin, an absolute path), this test can
    # prove nothing — say so rather than passing vacuously (rb-245).
    if r.returncode == 0:
        import pytest
        pytest.skip("uname shim did not take effect on this platform")

    assert r.returncode == 2, (
        f"a failing uname must exit 2 (cannot determine), not "
        f"{r.returncode} (which reads as an ordinary platform mismatch); "
        f"stderr={r.stderr!r}"
    )
    assert "cannot determine platform" in r.stderr


# --- --machine: box-level affinity () ------------------------------
#
# --os is coarse: it filters only when the target box is the only one of its OS.
# The motivating goal () must run on cc-04 while cc-07 is ALSO Linux,
# so `--os linux` filters nothing and the constraint stayed prose-only — which
# is invisible to goal-selector.py. Pre-flag measurement: that goal was a
# top-of-queue pick on cc-07 for three separate Bodies.
#
# THE TWO OR-LANE TESTS BELOW ARE THE LOAD-BEARING ONES, for the same reason the
# MSYSTEM pair above is. The script matches `uname -n` OR $MACHINE_ID, and each
# lane alone reintroduces one goal-HIDING mode: nodename-only misjudges a box
# whose MACHINE_ID is deliberately not its nodename, env-only disappears when the
# variable is not inherited across predicate.py's spawn. A future "simplification"
# to a single signal passes every positive test while restoring the fail-closed
# blast radius, so these two are the mutation-proof for the design, not decoration.


def this_machine():
    """The nodename the SCRIPT will see — read the same way it reads it.

    Deliberately not platform.node(): the script shells `uname -n`, and on MSYS
    the two can disagree in case. Comparing against the script's own signal keeps
    a harness discrepancy from reading as a script defect (guard-920).
    """
    r = subprocess.run(["uname", "-n"], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def test_machine_matches_this_box():
    me = this_machine()
    assert me, "uname -n returned nothing; cannot run the positive control"
    r = run(["--machine", me])
    assert r.returncode == 0, (
        f"--machine {me} should exit 0 on this box; rc={r.returncode} "
        f"stderr={r.stderr!r}"
    )


def test_machine_value_is_case_insensitive():
    me = this_machine()
    r = run(["--machine", me.upper()])
    assert r.returncode == 0, (
        f"--machine {me.upper()} should match (hostnames fold case); "
        f"rc={r.returncode}"
    )


def test_machine_mismatch_exits_one():
    r = run(["--machine", "definitely-not-this-box-9271"])
    assert r.returncode == 1, (
        f"an unknown machine id should exit 1 (mismatch), got {r.returncode}"
    )


def test_machine_matches_on_nodename_when_machine_id_is_unset():
    """OR lane 1 — scrubbing MACHINE_ID must not hide the goal.

    predicate.py invokes this through subprocess.run(shell=True); an env var is
    the signal most likely to go absent across that spawn. If detection is ever
    narrowed to $MACHINE_ID alone, this fails.
    """
    me = this_machine()
    r = run(["--machine", me], env={"MACHINE_ID": None})
    assert r.returncode == 0, (
        "--machine must still match via uname -n when MACHINE_ID is unset; "
        f"rc={r.returncode} stderr={r.stderr!r}"
    )


def test_machine_matches_on_machine_id_when_it_differs_from_nodename():
    """OR lane 2 — $MACHINE_ID is authoritative when it is not the nodename.

    The fleet SSOT (_session_telemetry._machine_id) prefers MACHINE_ID over the
    nodename, so a box configured that way must still answer YES. If detection is
    ever narrowed to `uname -n` alone, this fails.
    """
    alias = "zz-alias-box-4417"
    assert alias != this_machine(), "fixture alias collided with the real nodename"
    r = run(["--machine", alias], env={"MACHINE_ID": alias})
    assert r.returncode == 0, (
        "--machine must match via $MACHINE_ID even when it differs from uname -n; "
        f"rc={r.returncode} stderr={r.stderr!r}"
    )


def test_empty_machine_id_does_not_false_match():
    """An absent signal must never manufacture a YES.

    The empty-string lane is the one that could match something it should not,
    which is the direction that runs a goal on a box that cannot do the work.
    """
    r = run(["--machine", "some-other-box"], env={"MACHINE_ID": ""})
    assert r.returncode == 1, (
        f"empty MACHINE_ID must not satisfy an unrelated target; rc={r.returncode}"
    )


def test_neither_flag_is_a_usage_error():
    r = run([])
    assert r.returncode == 2
    assert "--machine" in r.stderr, (
        "the usage error must name --machine, or a caller cannot discover it"
    )


def test_both_flags_are_anded():
    me_os = this_platform()
    me_box = this_machine()
    other = next(p for p in ALL_PLATFORMS if p != me_os)

    assert run(["--os", me_os, "--machine", me_box]).returncode == 0
    assert run(["--os", me_os, "--machine", "not-this-box-3311"]).returncode == 1
    assert run(["--os", other, "--machine", me_box]).returncode == 1


def test_valueless_machine_flag_exits_two_instead_of_hanging():
    """Same guard-1224 hazard as --os: bare `shift 2` with $#==1 never shifts."""
    try:
        r = subprocess.run(
            [BASH, str(SCRIPT), "--machine"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "platform-check.sh --machine HUNG — use "
            "`shift $(( $# >= 2 ? 2 : 1 ))`, never bare `shift 2` (guard-1224)."
        )
    assert r.returncode == 2, (
        f"valueless --machine should be a usage error (2); got {r.returncode}"
    )


def test_machine_detection_failure_is_not_reported_as_a_mismatch(tmp_path):
    """A broken `uname` with no MACHINE_ID must exit 2 (cannot tell), never 1.

    Both codes hide the goal (predicate.py fails closed); the exit code is the
    only way a human can tell an unreadable box from a genuinely different one.
    """
    shim = tmp_path / "shim"
    shim.mkdir()
    fake = shim / "uname"
    fake.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
    fake.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    env.pop("MACHINE_ID", None)

    r = subprocess.run(
        [BASH, str(SCRIPT), "--machine", "any-box"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, env=env, timeout=30,
    )

    # Control: prove the shim took effect, rather than passing vacuously (rb-245).
    if r.returncode == 1 and "cannot determine machine identity" not in r.stderr:
        import pytest
        pytest.skip("uname shim did not take effect on this platform")

    assert r.returncode == 2, (
        f"an unreadable machine identity must exit 2, not {r.returncode}; "
        f"stderr={r.stderr!r}"
    )
    assert "cannot determine machine identity" in r.stderr


def test_predicate_evaluates_machine_precondition_end_to_end():
    """Drive the real predicate registry — with a case expected to PASS.

    box-affinity-preconditions.md mandates an expect-PASS control: evaluate_all
    returns a LIST, and reading .passed off the list yields None -> falsy, so an
    all-negative control set 'confirms' a stuck-false harness (guard-2298).
    """
    sys.path.insert(0, str(PROJECT_ROOT / "core" / "scripts"))
    from predicate import evaluate_all, _command_allowed  # noqa: E402

    me = this_machine()
    cmd = f"bash core/scripts/platform-check.sh --machine {me}"
    assert _command_allowed(cmd), "the machine gate must satisfy the predicate allowlist"

    passing = [{"id": "pc-box-match", "type": "command_succeeds", "command": cmd}]
    failing = [{"id": "pc-box-miss", "type": "command_succeeds",
                "command": "bash core/scripts/platform-check.sh --machine not-a-real-box-8823"}]

    got_pass = evaluate_all(passing, mode="fail_fast", include_skippable=False)
    assert got_pass, "evaluate_all returned no results for the matching predicate"
    assert all(r.passed for r in got_pass), (
        f"expect-PASS control failed: {[(r.reason, r.observed_value) for r in got_pass]}"
    )

    got_fail = evaluate_all(failing, mode="fail_fast", include_skippable=False)
    assert got_fail, "evaluate_all returned no results for the mismatching predicate"
    assert any(not r.passed for r in got_fail), (
        "a foreign machine id must FAIL the predicate — that is the direction "
        "that removes the goal from this box's candidates"
    )
