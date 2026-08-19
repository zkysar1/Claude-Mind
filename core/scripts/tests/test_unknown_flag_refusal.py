""" — hand-rolled wrappers must REFUSE an unknown flag.

WHY THIS FILE EXISTS
Fifteen wrappers were converted to `_argv_strict.sh` under g-115-4501/4504. Four
were skipped because they carry real flag tables of their own, so they kept the
`-*) PASSTHROUGH+=("$1"); shift;;` arm the conversion existed to kill. `PASSTHROUGH`
has no reader in any of the four — so an unrecognised flag vanished AND the next
token slid one slot left into a positional, writing the WRONG VALUE with exit
status 0.

THE FIFTH CASE IS A READ WRAPPER, AND ITS FAILURE MODE IS THE DANGEROUS ONE
(g-115-5214). `aspirations-query.sh` joined this file after the same shape was
found on the read side, where there is no positional to slide into — so the
swallowed flag does not corrupt a record, it makes the query answer a BROADER
question than the caller asked and still exit 0. Measured costs of that silence,
all three on this one wrapper: `--source` returned the union whichever value was
passed, so a caller who ran both and summed them published 456 fleet closes
against a true 228 (guard-2986); `--asp-id` returned 1539 rows where 44 matched,
35x wrong, and the bogus figure was used to accuse a correctly-working
goal-selector of suppressing a lane; `--goal-title-contains` returned 942 rows
byte-identical to no filter at all, 157x over-broad (guard-694). A wrong write is
caught by a read-back; an over-broad READ never looks like a failure, which is
why the read side needed the refusal at least as much as the write side.

WHY rc == 2 AND NOT `rc != 0` (this is the load-bearing assertion)
`_argv_strict.sh`'s header states it outright: the daemon transport path also
exits non-zero, so a test asserting `rc != 0` stays GREEN with the guard reverted.
Every case below pins rc == 2 exactly. Verified by mutation on 2026-08-04
(foxtrot; hostname LAPTOP-3IOFCNEO, uname -r 6.6.87.2-microsoft-standard-WSL2).
A scratch copy of `aspirations-update-goal.sh` with the `-*)` arm restored to the
passthrough form, run with the first case's argv, exited **1** — so
`assert rc != 0` would have stayed GREEN. Its stderr is the defect itself,
verbatim:

    {"error": "invalid_goal_id", "detail": "expected g-NNN-NN[N[N]], got 'SLID'"}

`SLID` is the token that FOLLOWED the unknown flag. It reached the GOAL_ID slot,
one position left of where the caller put it. Only `aspirations-update-goal.sh`
was mutated; the other three share the identical arm shape, so their revert
behaviour is inferred rather than measured.

WHY THE STORE IS PROVABLY UNTOUCHED
The goal names "leaves the store untouched" as the load-bearing half, because
this bug's signature is a SUCCESSFUL write. Three properties give that, and the
third is the one that makes the test safe to RUN:

  1. Ordering (test_refusal_precedes_runtime_source): `_argv_strict.sh` is sourced
     BEFORE `_runtime.sh` in all four, and the refusal `exit 2`s from the arg loop.
     No daemon client exists in the process yet, so no write can be attempted.
  2. No daemon-path stderr: the refusal message is the ONLY thing on stderr. A
     reverted guard reaches the daemon and its stderr looks entirely different.
  3. Every command targets a DELIBERATELY NONEXISTENT record id. If the guard is
     reverted and the daemon IS reached, the write fails as goal_not_found rather
     than mutating a live record. A regression test must not need to corrupt the
     store in order to detect corruption.

Point 3 is why these run against the real wrappers rather than a tmp world: the
production shape is what the defect lived in (guard-920), and the bogus id makes
that shape safe.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

# conftest.py already puts core/scripts/ on sys.path for collected tests; this
# insert matches the sibling pattern (test_body_heartbeat_writer.py:65) so the
# file also imports when run directly via `py -3 <this file>`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

# Bogus ids — see docstring point 3. Nothing here can name a live record.
BOGUS_GOAL = "g-99999-99999"
BOGUS_ASP = "asp-99999"
BOGUS_PIPE = "9999-99-99_g-115-4733-refusal-fixture"

# (wrapper, argv-after-wrapper). The unknown flag is FIRST in each, so the token
# that would slide into a positional slot under the old behaviour is the very
# next one — the exact clobber shape rb-538 describes.
CASES = [
    (
        "aspirations-update-goal.sh",
        ["--nonexistent-flag", "SLID", BOGUS_GOAL, "status", "completed"],
    ),
    (
        "aspirations-update.sh",
        ["--nonexistent-flag", "SLID", BOGUS_ASP, "status", "completed"],
    ),
    (
        "aspirations-add-goal.sh",
        ["--nonexistent-flag", "SLID", BOGUS_ASP],
    ),
    (
        "pipeline-update-field.sh",
        ["--nonexistent-flag", "SLID", BOGUS_PIPE, "status", "archived"],
    ),
    # READ wrapper (). Docstring point 3 (bogus id) applies with the
    # same intent but a different mechanism: this one cannot mutate anything, so
    # the bogus id is here to keep the REVERTED path cheap — a swallowed flag
    # would fall through to a real `--goal-field id <bogus>` query returning [],
    # rather than dragging the whole completed set over the daemon on every run.
    (
        "aspirations-query.sh",
        ["--nonexistent-flag", "SLID", "--goal-field", "id", BOGUS_GOAL],
    ),
    # First of the 22-wrapper rollout (). Same read-wrapper reasoning as
    # aspirations-query.sh above: it cannot mutate, and the bogus id keeps the
    # REVERTED path cheap — a swallowed flag falls through to `--id <bogus>`
    # returning nothing rather than dragging the whole pipeline over the daemon.
    #
    # WHY THIS ONE FIRST, and it is not arbitrary: the  caller scan
    # (core/scripts/unknown-flag-caller-scan.py) reported pipeline-read.sh READY
    # — 0 blocking callers, 0 unresolved, 0 hazards across 5 roots. The goal's
    # order is load-bearing (enumerate callers -> fix them -> THEN refuse),
    # because 's single caller passed TWO unrecognised flags and
    # refusing first would have disarmed a never-clobber guard downstream.
    (
        "pipeline-read.sh",
        ["--nonexistent-flag", "SLID", "--id", BOGUS_PIPE],
    ),
]

WRAPPERS = [c[0] for c in CASES]


def _run(wrapper, argv):
    env = dict(os.environ)
    # Force the local backend: guard-955 / rb-2983 — an own-cloud box derives the
    # S3 key from the env id, not from any tmp override, so a test write would
    # land on the PRODUCTION key. Nothing here should reach a backend at all; the
    # pin is the belt to the bogus-id braces.
    env["STORAGE_BACKEND"] = "local"
    # bash_cmd() and not a hand-built argv, because TWO guards apply and it is the
    # only form that satisfies both ():
    #   guard-580 — a .sh path handed to CreateProcess is not a valid Win32 image
    #     (OSError WinError 193). bash_cmd prepends the RESOLVED bash, never a bare
    #     "bash" argv[0], which CreateProcess would resolve to the System32 WSL
    #     launcher and block on forever (guard-1040's rc=124 hang — a strictly
    #     WORSE failure than the immediate error being fixed here).
    #   guard-581 — str(WindowsPath) yields backslashes, which bash treats as escape
    #     introducers and strips, silently producing a nonexistent path. bash_cmd
    #     passes the script through Path.as_posix().
    # A fix carrying only the first guard would trade WinError 193 for a silent
    # wrong-path failure; guard-581 names test harnesses as where that bites
    # hardest, because str() is already POSIX on Linux so the suite stays green.
    return subprocess.run(
        bash_cmd(SCRIPTS / wrapper, *argv),
        capture_output=True,
        text=True,
        input="",           # add-goal reads a JSON body from stdin; give it nothing
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_unknown_flag_exits_2(wrapper, argv):
    """rc is 2 EXACTLY, not merely non-zero (see module docstring)."""
    r = _run(wrapper, argv)
    assert r.returncode == 2, (
        f"{wrapper} returned {r.returncode}, expected 2.\n"
        f"rc=1 is what the REVERTED wrapper returns (daemon rejects the bogus "
        f"record) — pinning `!= 0` would not have caught that.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_unknown_flag_names_the_flag_and_the_defect(wrapper, argv):
    """The diagnostic must identify the offending token, not just fail."""
    r = _run(wrapper, argv)
    assert "unknown option" in r.stderr
    assert "--nonexistent-flag" in r.stderr, (
        "the refusal must echo the token the caller actually typed"
    )
    assert "g-115-4733" in r.stderr, (
        "the refusal cites its goal id so a future reader can find the mechanism"
    )


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_store_untouched_no_daemon_contact(wrapper, argv):
    """The refusal fires before any daemon client exists — nothing can be written.

    Asserted negatively: none of the daemon path's own markers appear. A reverted
    guard reaches `rt_call` and its stderr carries one of these.
    """
    r = _run(wrapper, argv)
    lowered = (r.stdout + r.stderr).lower()
    for marker in ("daemon", "rt_call", "goal_not_found", "not found", "http"):
        assert marker not in lowered, (
            f"{wrapper}: found daemon-path marker {marker!r} — the wrapper got past "
            f"the refusal and attempted a write.\nstderr={r.stderr!r}"
        )
    assert r.stdout == "", f"{wrapper} wrote to stdout: {r.stdout!r}"


@pytest.mark.parametrize("wrapper,argv", CASES, ids=WRAPPERS)
def test_refusal_enumerates_the_accepted_flags(wrapper, argv):
    """The refusal must name what IS accepted, not just what is not.

    A refusal that says "the flags in this script's case block" sends the caller
    to read source, which is barely better than the silent swallow it replaced.
    """
    r = _run(wrapper, argv)
    assert "Accepted flags:" in r.stderr, (
        f"{wrapper}: refusal does not enumerate the accepted set.\n{r.stderr}"
    )


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_help_exits_0_and_is_not_refused(wrapper):
    """`--help` must not be caught by the refusal.

    `--help` is a `-*` token, so adding the refusal REGRESSED it from a silent
    no-op into an exit-2 error — a regression the fix introduced rather than a
    defect it removed, and on the worst possible token: the g-115-4428 addendum
    measured that `--help` is the first thing a caller types at an unfamiliar
    wrapper. Help is a successful invocation, so rc is 0, not 2.
    """
    r = _run(wrapper, ["--help"])
    assert r.returncode == 0, (
        f"{wrapper} --help returned {r.returncode}, expected 0.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "refusing" not in (r.stdout + r.stderr).lower(), (
        f"{wrapper}: --help fell through to the unknown-flag refusal."
    )
    assert "Usage:" in r.stdout


# Wrappers that accept at least one real flag, so `<flag> --help` is a valid
# invocation. pipeline-update-field takes three positionals and NO flags, so
# `--source world --help` correctly refuses at --source before reaching --help —
# excluding it here is a scope statement, not a waiver.
HELP_AFTER_FLAG = [
    ("aspirations-update-goal.sh", ["--source", "world", "--help"]),
    ("aspirations-update.sh", ["--source", "world", "--help"]),
    ("aspirations-add-goal.sh", ["--source", "world", "--help"]),
    # NOTE the flag differs deliberately: --source is exactly what
    # aspirations-query.sh now REFUSES, so reusing it here would assert the
    # opposite of this test's intent. Its real flags are the four below.
    ("aspirations-query.sh", ["--goal-status", "completed", "--help"]),
]


@pytest.mark.parametrize(
    "wrapper,argv", HELP_AFTER_FLAG, ids=[c[0] for c in HELP_AFTER_FLAG]
)
def test_help_works_after_an_accepted_flag(wrapper, argv):
    """`--help` must work at ANY position, not only as argv[1].

    Found by fresh-eyes on this goal's own diff (F-001), because the test above
    only ever passed --help as the FIRST argument. aspirations-add-goal.sh keeps
    its help in a pre-parse block testing `$1` alone rather than in an
    `-h|--help)` case arm, so `--source world --help` fell through to the new
    refusal and exited 2. The $1-only form was harmless for as long as unknown
    flags were silently swallowed — the refusal is what converted a latent
    asymmetry into a live regression. That is the whole shape guard-2680 names:
    a refusal-only test suite cannot see what the refusal broke.
    """
    r = _run(wrapper, argv)
    assert r.returncode == 0, (
        f"{wrapper} {' '.join(argv)} returned {r.returncode}, expected 0.\n"
        f"stderr={r.stderr!r}"
    )
    assert "refusing" not in (r.stdout + r.stderr).lower()


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_refusal_precedes_runtime_source(wrapper):
    """Structural: the guard must be sourced BEFORE _runtime.sh.

    Behavioural tests alone cannot pin this. If someone moves the source line
    below `_runtime.sh`, the refusal still fires on a healthy box and every test
    above stays green — but on a box where the daemon fails to spawn, the wrapper
    now dies at the daemon step and the caller sees a transport error instead of
    the refusal. Same class as the `argv_strict` header's exit-2 warning: a guard
    whose failure mode is masked by an unrelated error is not a guard.
    """
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    strict = text.index('source "$CORE_ROOT/scripts/_argv_strict.sh"')
    runtime = text.index('source "$CORE_ROOT/scripts/_runtime.sh"')
    assert strict < runtime, (
        f"{wrapper}: _argv_strict.sh is sourced AFTER _runtime.sh — a daemon "
        f"failure would mask the unknown-flag refusal."
    )


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_no_silent_passthrough_arm_remains(wrapper):
    """No `-*)` arm may still append to PASSTHROUGH and shift.

    The defect is a SHAPE, not a message. This pins the shape so a future edit
    reintroducing a catch-all is caught even if it keeps the refusal text nearby.
    """
    text = (SCRIPTS / wrapper).read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "-*)":
            continue
        # Walk to the arm terminator, ignoring comments.
        body = []
        for follow in lines[i + 1 : i + 40]:
            body.append(follow)
            if ";;" in follow:
                break
        code = "\n".join(l for l in body if not l.strip().startswith("#"))
        assert "argv_strict_refuse_unknown" in code, (
            f"{wrapper}: the `-*)` arm at line {i+1} does not refuse.\n{code}"
        )
        assert "PASSTHROUGH" not in code, (
            f"{wrapper}: the `-*)` arm at line {i+1} still appends to PASSTHROUGH "
            f"— that array has no reader, so the flag vanishes silently."
        )


def test_run_never_hands_the_sh_path_to_the_os(monkeypatch):
    """ — the harness's OWN argv shape, pinned so a revert goes RED HERE.

    WHY THIS EXISTS, AND WHY THE OTHER 23 CASES CANNOT REPLACE IT
    This file spent its whole life exec'ing `.sh` paths directly. On Linux that
    works (the kernel honours the shebang), so every case above passed on the
    fleet boxes while all 23 died `OSError: [WinError 193]` on win32 — Windows
    has no shebang handling, so a shell script is not a valid process image.
    The suite was therefore GREEN ON THE DEFECT, and a green Linux run is not
    evidence the harness is correct (guard-1943: a suite certifies the FUNCTION,
    never the WIRING). Re-running the cases above on Linux after the fix proves
    nothing either — they passed before it. This case is the discrimination.

    It asserts the argv `_run` actually builds, not the outcome of running it,
    so it is platform-independent and fails on Linux the moment someone hands
    the script path back to the OS. Both guards are pinned separately because
    each has its own silent failure mode and satisfying only one is worse than
    obvious breakage:

      argv[0] — guard-580. A `.sh` is not a valid Win32 image; a BARE "bash"
        is worse still, because CreateProcess searches System32 before PATH and
        reaches the WSL launcher, which blocks forever on a dead LxssManager
        (guard-1040, rc=124 after the 120s timeout × 23 cases).
      argv[1] — guard-581. `str(WindowsPath)` yields backslashes, which bash
        treats as escape introducers and STRIPS, silently producing a path that
        does not exist. `.as_posix()` is the fix. On Linux `str()` and
        `as_posix()` are identical, which is exactly why this pin has to be an
        equality against `as_posix()` rather than a backslash scan — a scan
        would be vacuous on the only platform most runs happen on.
    """
    captured = {}

    class _Dummy:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _Dummy()

    # monkeypatch (not a module-level stub) per guard-1165 — this must not leak
    # into any other test in the session.
    monkeypatch.setattr(subprocess, "run", _fake_run)
    _run("aspirations-update-goal.sh", ["--help"])

    argv = captured["argv"]
    script = SCRIPTS / "aspirations-update-goal.sh"

    assert argv[0] != str(script), (
        "argv[0] is the .sh path itself — this is the g-306-231 defect verbatim. "
        "On win32 CreateProcess raises OSError [WinError 193]; on Linux it "
        "silently works, which is how this survived. Use bash_cmd()."
    )
    assert Path(argv[0]).name.startswith("bash"), (
        f"argv[0]={argv[0]!r} is not a bash binary — guard-580 requires the "
        "RESOLVED interpreter as argv[0]."
    )
    assert argv[1] == script.as_posix(), (
        f"argv[1]={argv[1]!r} is not the .as_posix() form of the script path. "
        "guard-581: backslashes reaching bash are stripped as escapes, yielding "
        "a nonexistent path. This assertion is identical to str() on Linux and "
        "only bites on Windows — that asymmetry is the point."
    )
    assert argv[2:] == ["--help"], (
        f"trailing args were not passed through verbatim: {argv[2:]!r}"
    )
