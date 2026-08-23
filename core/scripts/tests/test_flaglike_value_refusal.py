"""test_flaglike_value_refusal.py — the VALUE-position half of the argv swallow.

THE DEFECT, one position over from the one test_unknown_flag_refusal.py covers.
The unknown-FLAG refusal only sees tokens that reach the `case` statement. A
value-taking flag consumes `"${2-}"` unconditionally, so when its value is
MISSING it eats the NEXT FLAG as its value and that token never reaches the
refusal at all — the guard structurally cannot catch this one. The command then
answers a DIFFERENT QUESTION and exits 0.

MEASURED on experience-read.sh before the guard was added (cc-07, 2026-08-21):

    experience-read.sh --category --summary   ->  rc=0, stdout `[]`, EMPTY stderr

It had queried for a category literally named "--summary", found none, and
reported success. A caller reads that as "no experiences in this category".
Positive control from the same session: a real category returns 614,633 bytes,
so the wrapper works and the `[]` was genuinely the swallow.

WHY THIS FILE EXISTS AT ALL. `argv_strict_refuse_flaglike_value` shipped with
g-115-5128 and, measured before writing this, had ZERO test references anywhere
under core/scripts/tests or core/tests — including on aspirations-query.sh, the
only wrapper that had adopted it. So the value-position defense was live and
unpinned: reverting it would have reddened nothing. Both adopters are covered
here for that reason, and a second wrapper is also what keeps the assertions
from being hardcoded to one message.

TABLE-DRIVEN ON PURPOSE. Twelve wrappers have adopted the unknown-flag refusal
and carry value-arg sites with no flaglike guard (measured 2026-08-21). Each new
adoption should be one row in CASES, not a new file.

rc == 2 EXACTLY, never merely non-zero — the wrappers' daemon path exits 1, so a
`!= 0` assertion stays green against a fully reverted guard. That is the same
contract the sibling file pins and the reason _argv_strict.sh's header calls out
exit 2 as part of it.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

# A value that cannot name a live record, so the NON-refusing cases stay cheap
# and cannot drag a real population over the daemon.
BOGUS_VALUE = "flaglike-fixture-nonexistent"

# (wrapper, value-taking flag, an ACCEPTED flag of that same wrapper)
#
# The third element must be a flag the wrapper ACCEPTS. A bogus flag would be
# caught by the unknown-flag refusal further along and the test would pass for
# the wrong reason — it is precisely the accepted-flag case that the unknown-flag
# guard cannot see, because the token is eaten before it is ever classified.
CASES = [
    #  adoption (2026-08-21). All five of this wrapper's value-arg
    # sites; --most-retrieved/--least-retrieved/--recent are deliberately absent
    # because they already guard numerically (they take $2 only if it matches
    # ^[0-9]+$), so they were never exposed and were not touched.
    ("experience-read.sh", "--id", "--summary"),
    ("experience-read.sh", "--category", "--summary"),
    ("experience-read.sh", "--goal", "--summary"),
    ("experience-read.sh", "--hypothesis", "--summary"),
    ("experience-read.sh", "--type", "--summary"),
    #  adoption — the pre-existing one, unpinned until this file.
    ("aspirations-query.sh", "--title-contains", "--full"),
    ("aspirations-query.sh", "--goal-status", "--full"),
]

IDS = [f"{w}{f}" for w, f, _ in CASES]


def _run(wrapper, argv):
    env = dict(os.environ)
    # guard-955 / rb-2983: an own-cloud box derives the S3 key from the env id,
    # not from any tmp override, so an unpinned test write lands on the
    # PRODUCTION key. Nothing here should reach a backend, but the pin is the
    # belt to the bogus-value braces.
    env["STORAGE_BACKEND"] = "local"
    # bash_cmd() rather than a hand-built argv: guard-580 (never a bare "bash"
    # argv[0]) and guard-581 (Path.as_posix, never str(WindowsPath)).
    return subprocess.run(
        bash_cmd(SCRIPTS / wrapper, *argv),
        capture_output=True,
        text=True,
        input="",
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


@pytest.mark.parametrize("wrapper,flag,poison", CASES, ids=IDS)
def test_flaglike_value_exits_2(wrapper, flag, poison):
    """rc is 2 EXACTLY. A reverted guard answers the wrong question with rc=0."""
    r = _run(wrapper, [flag, poison])
    assert r.returncode == 2, (
        f"{wrapper} {flag} {poison} returned {r.returncode}, expected 2.\n"
        f"rc=0 is what the REVERTED wrapper returns — it silently queries for a "
        f"value literally named {poison!r} and reports success, so pinning "
        f"`!= 0` would not have caught it.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )


@pytest.mark.parametrize("wrapper,flag,poison", CASES, ids=IDS)
def test_refusal_names_the_flag_and_the_swallowed_value(wrapper, flag, poison):
    """The diagnostic must identify BOTH, or it cannot be acted on.

    Naming only the flag leaves the reader guessing which token was eaten;
    naming only the value does not say which slot ate it.
    """
    r = _run(wrapper, [flag, poison])
    assert flag in r.stderr, f"stderr does not name {flag}: {r.stderr!r}"
    assert poison in r.stderr, f"stderr does not name {poison}: {r.stderr!r}"


@pytest.mark.parametrize("wrapper,flag,poison", CASES, ids=IDS)
def test_refusal_writes_nothing_to_stdout(wrapper, flag, poison):
    """A refusal that also printed a result would be read as an answer.

    The whole failure mode is a caller trusting plausible output, so the refusal
    must leave stdout empty rather than emit `[]` alongside the complaint.
    """
    r = _run(wrapper, [flag, poison])
    assert r.stdout.strip() == "", f"{wrapper} printed on refusal: {r.stdout!r}"


# --- the guard must NOT fire on the legitimate cases ------------------------

@pytest.mark.parametrize("wrapper,flag,poison", CASES, ids=IDS)
def test_ordinary_value_is_not_refused(wrapper, flag, poison):
    """ANTI-VACUITY. Without this, every assertion above survives a mutation
    that refuses unconditionally — which would break every real caller while
    turning this file green.
    """
    r = _run(wrapper, [flag, BOGUS_VALUE])
    assert r.returncode != 2, (
        f"{wrapper} {flag} {BOGUS_VALUE!r} was refused (rc=2). A plain value is "
        f"not flag-like and must pass through.\nstderr={r.stderr!r}"
    )


def test_empty_value_is_not_claimed_by_this_guard():
    """A MISSING value at end-of-argv is a DIFFERENT defect, and the helper
    deliberately returns 0 for it so each wrapper's own "filter required" error
    keeps reporting it. Pinned so a future reader does not "complete" the guard
    by claiming the empty case and replacing a clear error with a vaguer one —
    the helper's own comment asks for exactly that restraint.
    """
    r = _run("experience-read.sh", ["--category"])
    assert r.returncode != 2, (
        "empty value was refused by the flaglike guard; it belongs to the "
        f"wrapper's own missing-filter error.\nstderr={r.stderr!r}"
    )


def test_guard_discriminates_across_both_adopters():
    """One assertion that fails if the guard is stubbed either way.

    Kept separate from the parametrized cases so the file states its own
    non-degeneracy in a single place (guard-1793: an aggregate supplements the
    per-case assertions, it never substitutes for them).
    """
    refused = _run("experience-read.sh", ["--category", "--summary"]).returncode
    allowed = _run("experience-read.sh", ["--category", BOGUS_VALUE]).returncode
    q_refused = _run("aspirations-query.sh", ["--title-contains", "--full"]).returncode
    assert refused == 2 and q_refused == 2 and allowed != 2, (
        f"degenerate: refused={refused} q_refused={q_refused} allowed={allowed}")
