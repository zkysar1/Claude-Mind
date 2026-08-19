"""aspirations-query.sh must refuse a FLAG sitting in a VALUE slot ().

THE DEFECT THIS PINS, and why the existing refusal could not catch it.
`test_unknown_flag_refusal.py` pins the FLAG position: an unrecognised `-*` token
exits 2 instead of being swallowed. That guard is structurally blind to the VALUE
position, and not by oversight — a token consumed as some flag's value never
reaches the `-*)` arm at all. So a value-taking flag whose value is MISSING eats
the NEXT FLAG as its value and the query answers a different question with rc=0:

    --title-contains 'directive-lane' --goal-field id --full   -> filter id == "--full" -> []
    --title-contains 'directive-lane' --goal-status --full     -> status == "--full"    -> []

Both are silent. A zero from this wrapper is indistinguishable from a genuinely
empty corpus, which is what makes it expensive: it reads as an authoritative
negative and ends an investigation.

WHAT THIS FILE ALSO PINS, DELIBERATELY: that `--goal-field` COMPOSES with the
other filters. g-115-5128 was filed believing the two filters conflicted and that
`--goal-field` "does not narrow the result set, it EMPTIES it". Measured on
cc-07 2026-08-10, that premise is FALSE — `--title-contains X --goal-field status
pending` returned 12 against a 20-row base. The filed reading was taken before
the unknown-flag refusal existed, when `--source` and `--json` were still silently
discarded, so the invocation that produced the zero was really
`--goal-field id --json` with `--json` eaten as the VALUE. Without
`test_goal_field_narrows_rather_than_empties` below, a future reader meets the
same zero, reads the same goal, and "fixes" a composition bug that was never
there. Pinning a premise as false is as load-bearing as pinning the fix.

Every test uses the PRODUCTION invocation shape (guard-920) and pins rc == 2
EXACTLY, never merely non-zero: the daemon transport path also exits non-zero, and
`no filter supplied` exits 1.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

# conftest.py already puts core/scripts/ on sys.path for collected tests; this
# insert matches the sibling pattern so the file also imports when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

WRAPPER = "aspirations-query.sh"

# Every slot that takes a value, paired with an invocation whose value position
# holds a flag. `--full` is the eaten token in each because it is a REAL flag of
# this wrapper — the shape a caller actually produces by forgetting an argument,
# not a synthetic token (guard-920).
FLAGLIKE_CASES = [
    ("goal-field-value", ["--title-contains", "x", "--goal-field", "id", "--full"]),
    ("goal-field-name", ["--title-contains", "x", "--goal-field", "--full", "pending"]),
    ("goal-status", ["--title-contains", "x", "--goal-status", "--full"]),
    ("title-contains", ["--title-contains", "--full", "--goal-status", "pending"]),
]
CASE_IDS = [c[0] for c in FLAGLIKE_CASES]

# Invocations that MUST keep working. guard-2680: a new refusal arm's blast radius
# has to be enumerated and the non-refused path asserted, or the arm silently
# becomes a regression — that guard exists because a catch-all refusal ate --help.
MUST_STILL_WORK = [
    ("help-long", ["--help"], 0),
    ("help-short", ["-h"], 0),
    ("title-contains-alone", ["--title-contains", "Fix"], 0),
    ("goal-status-alone", ["--goal-status", "completed"], 0),
    ("goal-field-two-arg", ["--goal-field", "status", "completed"], 0),
    ("status-plus-full", ["--goal-status", "completed", "--full"], 0),
    # The shape mind_api/bench/bench.py actually invokes.
    ("bench-call-shape", ["--goal-status", "pending", "--title-contains", "encoding"], 0),
]
WORK_IDS = [c[0] for c in MUST_STILL_WORK]


def _run(*argv):
    env = dict(os.environ)
    # guard-955 / rb-2983: an own-cloud box derives the S3 key from the env id,
    # not from any tmp override, so an unpinned test write lands on the PRODUCTION
    # key. Every invocation here is read-only, but the pin is not conditional on
    # that staying true.
    env["STORAGE_BACKEND"] = "local"
    # bash_cmd() rather than a hand-built argv: guard-580 (a .sh path handed to
    # CreateProcess is not a valid Win32 image, and a bare "bash" argv[0] resolves
    # to the System32 WSL launcher and hangs) plus guard-581 (str(WindowsPath)
    # yields backslashes that bash strips as escapes).
    return subprocess.run(
        bash_cmd(SCRIPTS / WRAPPER, *argv),
        capture_output=True,
        text=True,
        input="",
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


def _rows(result):
    """Parse the wrapper's stdout into a row list, or fail loudly."""
    payload = json.loads(result.stdout)
    return payload if isinstance(payload, list) else payload.get("goals", [])


@pytest.mark.parametrize("name,argv", FLAGLIKE_CASES, ids=CASE_IDS)
def test_flaglike_value_exits_2(name, argv):
    """rc is 2 EXACTLY. rc=0 is the unfixed wrapper; rc=1 is 'no filter supplied'."""
    r = _run(*argv)
    assert r.returncode == 2, (
        f"{name}: returned {r.returncode}, expected 2.\n"
        f"rc=0 is precisely the defect — the flag was eaten as a value and the "
        f"query answered a different question successfully.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("name,argv", FLAGLIKE_CASES, ids=CASE_IDS)
def test_flaglike_value_emits_no_rows(name, argv):
    """A refusal must not ALSO print a result set — that would be readable as data."""
    r = _run(*argv)
    assert r.stdout.strip() == "", (
        f"{name}: refused but still wrote to stdout, which a caller piping this "
        f"into a parser would consume as an answer.\nstdout={r.stdout!r}"
    )


@pytest.mark.parametrize("name,argv", FLAGLIKE_CASES, ids=CASE_IDS)
def test_refusal_names_the_token_and_the_slot(name, argv):
    """Naming only one of the two leaves the caller guessing which arg to fix."""
    r = _run(*argv)
    assert "--full" in r.stderr, (
        f"{name}: refusal does not name the offending token.\nstderr={r.stderr!r}"
    )
    slot = argv[argv.index("--full") - 1] if "--full" in argv else ""
    # The slot is named either directly or via the `--goal-field <name>/<value>`
    # form; assert the owning flag appears so the caller knows where to look.
    owner = "--goal-field" if "--goal-field" in argv[:argv.index("--full") + 1] else slot
    assert owner in r.stderr, (
        f"{name}: refusal names the token but not the flag whose slot ate it "
        f"(expected {owner!r}).\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("name,argv", FLAGLIKE_CASES, ids=CASE_IDS)
def test_refusal_offers_the_substring_workaround(name, argv):
    """A refusal is only better than a silent swallow if the caller can act on it.

    306 of 6086 live goal titles contain a `--token`, so refusing dash-prefixed
    search terms blocks a real use. That is an acceptable trade ONLY because the
    filters are substring matches and dropping the dashes is lossless — which the
    message has to say, or the caller is simply stuck.
    """
    r = _run(*argv)
    assert "substring" in r.stderr.lower(), (
        f"{name}: refusal does not tell the caller how to pass a literal value "
        f"beginning with '-'.\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("name,argv", FLAGLIKE_CASES, ids=CASE_IDS)
def test_refusal_enumerates_accepted_flags(name, argv):
    r = _run(*argv)
    assert "Accepted flags:" in r.stderr, (
        f"{name}: refusal does not enumerate the accepted set, so it names the "
        f"defect but not the fix.\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("name,argv,want_rc", MUST_STILL_WORK, ids=WORK_IDS)
def test_non_refused_paths_still_work(name, argv, want_rc):
    """guard-2680: enumerate the arm's blast radius and pin what must still work."""
    r = _run(*argv)
    assert r.returncode == want_rc, (
        f"{name}: {argv} returned {r.returncode}, expected {want_rc}. The new "
        f"value-slot refusal has caught an invocation it was never meant to.\n"
        f"stdout={r.stdout[:400]!r}\nstderr={r.stderr[:400]!r}"
    )


def test_goal_field_narrows_rather_than_empties():
    """'s PREMISE, pinned as FALSE.

    The goal asserts `--goal-field` "does not narrow the result set, it EMPTIES
    it". If that were true this returns []. It is not true, and this test is what
    stops the false premise being re-derived from the goal record later.

    Built as its own positive control (rb-245): the base query is asserted
    non-empty FIRST, so a genuinely empty corpus SKIPS rather than passing
    vacuously. A zero here would otherwise be indistinguishable from the very
    defect under test — which is the whole lesson of the goal.
    """
    base = _run("--goal-status", "completed")
    assert base.returncode == 0, f"base query failed: {base.stderr!r}"
    base_rows = _rows(base)
    if not base_rows:
        pytest.skip("no completed goals in this store — composition test is vacuous")

    composed = _run("--goal-status", "completed", "--goal-field", "status", "completed")
    assert composed.returncode == 0, f"composed query failed: {composed.stderr!r}"
    composed_rows = _rows(composed)

    assert composed_rows, (
        "--goal-field EMPTIED a result set that --goal-status alone filled "
        f"({len(base_rows)} rows). That is g-115-5128's stated premise, and it was "
        "measured FALSE on 2026-08-10 — if this fires, either the premise became "
        "true or the composition genuinely regressed. Do not 'fix' it by reading "
        "the goal description; re-measure first."
    )
    assert len(composed_rows) <= len(base_rows), (
        f"--goal-field WIDENED the set ({len(composed_rows)} > {len(base_rows)}); "
        "a filter must never add rows."
    )
