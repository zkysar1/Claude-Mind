""" — permissions-add.sh must treat --help as a QUERY, not an invocation.

WHAT WAS WRONG
The script (63 lines) had no `case`, no `while`, no getopts, and never referenced
"$@" at all. Every argument was silently dropped and control fell through to the
write path, so `bash core/scripts/permissions-add.sh --help` did not print usage —
it MUTATED .claude/settings.local.json, the constitutional anchor that CLAUDE.md
says the agent must never edit outside a user-authorized maintenance path.

WHY THAT TOKEN IS THE WORST ONE TO GET WRONG (guard-2680): `--help` is the first
thing a caller types at an unfamiliar script, and they reach for it precisely when
they do NOT yet know what it does. So the least-informed possible caller got an
unconfirmed write to the most-protected file in the repo. It fired on the first
attempt, when someone ran it expecting usage text.

WHY THIS TEST NEEDS A TEMP PROJECT ROOT, which is the whole design of the file.
A regression test must not need to corrupt the store in order to detect
corruption (the standard test_unknown_flag_refusal.py sets). Here that standard
bites unusually hard: the thing this guard protects IS the constitutional anchor,
so a test that ran the REVERTED script against the real tree would write the very
file the fix exists to defend.

The obvious lever does NOT work, and was measured before being discarded:
pointing MIND_AGENT at a nonexistent agent does NOT make path resolution fail
closed — _paths.sh still resolves WORLD_DIR/META_DIR from the repo's .mind-data/,
byte-identically to a real agent, so the write proceeds anyway.

What DOES work is that permissions-add.sh derives PROJECT_ROOT from its own
location ($CORE_ROOT/..). Copying it into a temp tree redirects the write to
<tmp>/.claude/settings.local.json — a real exercise of the real write path,
contained. That is what makes the no-argument case below safe to run, and it is
the only case here that writes anything at all.

WHY rc == 2 EXACTLY AND NOT `rc != 0`
_argv_strict.sh's header states it: other failure paths in these wrappers also
exit non-zero, so `assert rc != 0` can stay green with the guard reverted. Here
the reverted script ALSO exits 2 whenever paths are unresolved, so rc alone
cannot separate the two — every refusal case pins BOTH rc == 2 and the refusal
TEXT on stderr, and the --help cases pin rc == 0, which the reverted script
cannot produce.
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "core" / "scripts"))
from _runtime_bash import bash_cmd  # noqa: E402  (guard-580: never a bare "bash")

SCRIPT_REL = "core/scripts/permissions-add.sh"
# The minimum set that lets the wrapper resolve paths and run its helper.
COPIED = [
    "core/scripts/permissions-add.sh",
    "core/scripts/permissions-add.py",
    "core/scripts/_paths.sh",
    "core/scripts/_argv_strict.sh",
]


@pytest.fixture
def harness(tmp_path):
    """A temp PROJECT_ROOT whose .claude/ the wrapper writes instead of ours."""
    for rel in COPIED:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
    # _paths.sh resolves WORLD_DIR/META_DIR from a repo-local .mind-data/ when
    # present; without it the wrapper exits 2 before reaching the write, and the
    # no-argument case below could not tell a working write from a broken path.
    (tmp_path / ".mind-data" / "world").mkdir(parents=True)
    (tmp_path / ".mind-data" / "meta").mkdir(parents=True)
    return tmp_path


def run(harness, *args):
    return subprocess.run(
        bash_cmd(harness / SCRIPT_REL, *args),
        capture_output=True,
        text=True,
        cwd=str(harness),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(harness),
            "MIND_AGENT": "alpha",
            "STORAGE_BACKEND": "local",
        },
    )


def anchor(harness):
    return harness / ".claude" / "settings.local.json"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_is_a_query_and_writes_nothing(harness, flag):
    """The defect itself: this used to create settings.local.json and exit 0."""
    r = run(harness, flag)
    assert r.returncode == 0, f"--help must exit 0, got {r.returncode}: {r.stderr}"
    assert "Usage:" in r.stdout
    assert not anchor(harness).exists(), (
        f"{flag} WROTE the anchor — this is the g-115-8770 defect"
    )


@pytest.mark.parametrize("flag", ["--bogus", "--dry-run", "--force", "--version"])
def test_unknown_flags_are_refused_with_rc_2_and_write_nothing(harness, flag):
    """--dry-run is the load-bearing member: a caller typing it expects NO write,
    and before this guard it got one. Refusing beats silently ignoring here."""
    r = run(harness, flag)
    assert r.returncode == 2, f"expected rc 2, got {r.returncode}: {r.stderr}"
    assert "unknown option" in r.stderr
    # The refusal must be actionable, not a pointer at the source (guard-2680).
    assert "Accepted flags:" in r.stderr
    assert not anchor(harness).exists(), f"{flag} WROTE the anchor"


def test_no_arguments_still_writes(harness):
    """The non-refused path. A refusal-only suite is structurally unable to see a
    false positive (guard-2680), and the sole production call site
    (start-uninitialized-ceremony.md) invokes this with no arguments at all — so
    this case, not the refusals, is what a regression here would break."""
    r = run(harness)
    assert r.returncode == 0, f"no-arg run must still succeed: {r.stderr}"
    assert anchor(harness).exists(), "the write path was broken by the arg loop"
    assert anchor(harness).stat().st_size > 0


def test_real_constitutional_anchor_is_never_touched_by_this_suite(harness):
    """Belt-and-braces on the containment claim in this file's docblock: if a
    future edit reintroduces a real-tree path, this fails loudly instead of
    silently rewriting the anchor."""
    real = REPO / ".claude" / "settings.local.json"
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    run(harness, "--help")
    run(harness, "--bogus")
    run(harness)
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before
