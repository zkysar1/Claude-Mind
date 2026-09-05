"""`--since` VALUE validation across the wrappers that own the flag ().

g-115-4428 hardened `board-read.sh` against an unknown FLAG NAME; g-115-3775
hardened the board `since` VALUE at the daemon (ISO accepted, anything else a
400). g-306-431 asked whether the sibling wrappers that take `--since` carry the
same value-vs-flag gap. Two did:

  * ``presence-read.sh`` compares ``--since`` LEXICOGRAPHICALLY against each
    record's ISO ``ts``. That is order-preserving for an ISO prefix and
    meaningless otherwise, so an unvalidated value failed at rc=0 in the
    direction that looks like an answer -- measured on cc-04 2026-09-04 against
    alpha's 4081-record stream, ``--since 30h`` (the duration form
    ``board-read.sh`` accepts) returned 0 rows, as did ``zzz``.

  * ``mirror-integrity-check.sh`` sent ``tree-edit-since.py``'s stderr to
    /dev/null, converting that tool's loud refusal into an empty node list which
    the next branch reported as "0 tree nodes ... nothing to read".

The second fix rests on an assumption about ``tree-edit-since.py``'s exit codes
that is NOT self-evident and IS the thing that would silently break it, so it is
pinned here beside the behaviour it supports.
"""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parents[1]
sys.path.insert(0, str(SCRIPTS))

from _runtime_bash import bash_cmd  # noqa: E402

PRESENCE_READ = SCRIPTS / "presence-read.sh"
TREE_EDIT_SINCE = SCRIPTS / "tree-edit-since.py"
MIRROR_CHECK = SCRIPTS / "mirror-integrity-check.sh"

# An agent that cannot have a presence file. Keeps every case below hermetic:
# the validation runs BEFORE the presence-file lookup, so the two branches are
# distinguishable by exit code alone with nothing written anywhere.
ABSENT_AGENT = "no-such-agent-g306431"


def _presence_read(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        bash_cmd(str(PRESENCE_READ), "--agent", ABSENT_AGENT, *args),
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )


# --- presence-read.sh: an unparseable --since VALUE is refused, not silent ---

@pytest.mark.parametrize("bad", [
    "30h",          # the duration form board-read.sh accepts -- the live confusion
    "24h",
    "7d",
    "zzz",
    "not-a-time",
    "2026/09/01",   # right idea, wrong separator
    "20260901",     # right idea, no separators
])
def test_unparseable_since_is_refused_not_silently_applied(bad):
    r = _presence_read("--since", bad)
    # rc 1 == invalid args per the script's own exit-code contract; rc 2 would
    # mean it fell through to the presence-file lookup, i.e. the value was
    # accepted. rc 0 would be the original defect: a confident empty result.
    assert r.returncode == 1, (
        f"--since {bad!r} should be refused as an invalid arg, got rc={r.returncode}; "
        f"stdout={r.stdout!r} stderr={r.stderr!r}")
    assert "is not an ISO-8601 timestamp" in r.stderr
    # The refusal must NAME the accepted forms -- a refusal that does not is a
    # dead end for the caller who just guessed wrong.
    assert "YYYY-MM-DD" in r.stderr


@pytest.mark.parametrize("good", [
    "2026-09-01",
    "2026-09-01T06:30",
    "2026-09-01T06:30:00",
    "2026-09-01 06:30:00",   # space separator, the other ISO-8601 form
])
def test_valid_iso_since_is_accepted(good):
    """Positive control. Without this the parametrised refusal above would pass
    just as happily against a validator that rejected EVERYTHING."""
    r = _presence_read("--since", good)
    # Falls through validation to the presence-file lookup, which is rc 2 for an
    # agent that has no stream. Anything else means the value was refused.
    assert r.returncode == 2, (
        f"--since {good!r} should pass validation, got rc={r.returncode}; "
        f"stderr={r.stderr!r}")
    assert "is not an ISO-8601 timestamp" not in r.stderr


def test_absent_since_is_still_allowed():
    r = _presence_read()
    assert r.returncode == 2, f"no --since should reach the file lookup, got rc={r.returncode}"


# --- the assumption mirror-integrity-check.sh's fix rests on ----------------

def test_tree_edit_since_rc_is_overloaded_so_callers_must_read_stderr():
    """``tree-edit-since.py`` exits 1 for BOTH "no matching nodes" and "bad
    timestamp" -- documented in its own module docstring as fail-open. A caller
    therefore CANNOT discriminate on the exit code, which is why
    mirror-integrity-check.sh greps stderr for 'bad timestamp'.

    If this ever becomes discriminable by rc, this test fails and the caller can
    be simplified. If the stderr WORDING changes, the caller silently stops
    detecting refusals -- so the marker string is pinned here too.
    """
    bad = subprocess.run(
        [sys.executable, str(TREE_EDIT_SINCE), "30h", "--list"],
        capture_output=True, text=True, cwd=str(REPO), timeout=120)
    # A window far enough ahead that no node can have been modified after it.
    empty = subprocess.run(
        [sys.executable, str(TREE_EDIT_SINCE), "2099-01-01T00:00:00", "--list"],
        capture_output=True, text=True, cwd=str(REPO), timeout=120)

    assert bad.returncode == empty.returncode == 1, (
        "the premise of the stderr discriminator is that both paths exit 1; "
        f"bad={bad.returncode} empty={empty.returncode}")
    assert "bad timestamp" in bad.stderr, (
        "mirror-integrity-check.sh greps for this exact marker; "
        f"got stderr={bad.stderr!r}")
    assert "bad timestamp" not in empty.stderr, (
        "the empty case must NOT match the refusal marker, or every empty "
        f"window reports BLIND; got stderr={empty.stderr!r}")


def test_mirror_integrity_check_discriminates_on_stderr_not_rc():
    """Source-level pin with a real failure mode behind it: a first version of
    this fix branched on the rc and labelled every empty window a refusal."""
    src = MIRROR_CHECK.read_text(encoding="utf-8")
    assert "bad timestamp" in src, (
        "mirror-integrity-check.sh must discriminate tree-edit-since.py's "
        "refusal by its stderr marker")
    assert 'TES_ERR' in src
