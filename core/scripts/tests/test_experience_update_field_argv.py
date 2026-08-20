""" — experience-update-field.sh joins the argv_strict_parse family.

THE DEFECT. This was the LAST unguarded member of the six <id> <field> <value>
siblings: it read $1/$2/$3 blindly, so `<id> <field> --value-file <path>` stored
the literal string "--value-file" as the field value with rc=0 — the write-side
swallow class of g-115-4501 (a path overwrote guard-1615's 1400-char rule on a
sibling), with the extra twist that here the FLAG TEXT itself became the value.

THE CONTRACT PINNED HERE is the argv_strict_parse family's (guardrails-,
reasoning-bank-, pattern-signatures-, spark-questions-update-field), NOT the
newer refuse_unknown contract test_unknown_flag_refusal.py pins: parse prints
the g-115-4501 citation, writes usage to STDERR, and has no --help arm. Those
are its four siblings' measured behaviors today; uniformizing the parse family
onto the newer contract is separate work, and pinning the newer contract here
would fail against the exact shape the siblings deliberately share.

Hermetic: bogus record id + STORAGE_BACKEND=local (guard-955) + the refusal
fires in the parse, BEFORE _runtime.sh is sourced — no daemon client exists.
rc==2 is pinned SPECIFICALLY (the _argv_strict contract): the daemon path also
exits non-zero, so `!= 0` would stay green on revert.
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2].parent
SCRIPTS = PROJECT_ROOT / "core" / "scripts"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _runtime_bash import bash_cmd  # noqa: E402

WRAPPER = SCRIPTS / "experience-update-field.sh"
BOGUS = "exp-refusal-fixture-999999"


def _run(argv):
    env = dict(os.environ)
    env["STORAGE_BACKEND"] = "local"
    return subprocess.run(
        bash_cmd(WRAPPER, *argv),
        capture_output=True,
        text=True,
        input="",
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )


def test_unknown_flag_refused_rc2_names_token_and_lineage():
    r = _run(["--nonexistent-flag", "SLID", BOGUS, "tags", "x"])
    assert r.returncode == 2, (
        f"expected parse-family refusal rc=2, got {r.returncode}.\n"
        f"The REVERTED wrapper binds $1/$2/$3 blindly and proceeds to the "
        f"daemon path, which exits 1 on the bogus id — pinning !=0 would not "
        f"catch the revert.\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "--nonexistent-flag" in r.stderr, "refusal must echo the typed token"
    assert "g-115-4501" in r.stderr, "parse family cites the write-clobber goal"
    assert r.stdout == ""


def test_extra_positional_refused_rc2():
    r = _run([BOGUS, "tags", "x", "extra-word"])
    assert r.returncode == 2, (
        f"expected extra-positional refusal rc=2, got {r.returncode}.\n"
        f"stderr={r.stderr!r}"
    )
    assert "extra-word" in r.stderr, "refusal must name the first extra token"


def test_flag_text_can_no_longer_become_the_stored_value():
    """The incident shape verbatim: --value-file as $3. Pre-fix, the literal
    string '--value-file' was stored as the field value with rc=0."""
    r = _run([BOGUS, "tags", "--value-file", "/nonexistent/refusal-fixture"])
    assert r.returncode == 2, (
        f"expected rc=2 (either unknown-flag refusal or value-file-not-found), "
        f"got {r.returncode}.\nA rc of 0/1 means the flag text reached the "
        f"daemon as a VALUE again.\nstdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    lowered = (r.stdout + r.stderr).lower()
    for marker in ("daemon", "rt_call", "http"):
        assert marker not in lowered, (
            f"daemon-path marker {marker!r} present — the parse handed off "
            f"instead of refusing.\nstderr={r.stderr!r}"
        )
