"""test_owncloud_pull_only_scope_coverage.py — regression for .

`owncloud-pull.sh --only <name>` narrows the pull to named CONTINUITY files.
A name that is not in the continuity set matches nothing, and the wrapper used
to report that outcome as::

    [owncloud-pull] agent=alpha ... pulled=0 in_sync=0 scanned=0 ... errors=0
    rc=0

`scanned` does differ between "looked at N files" and "looked at nothing" — the
goal's own framing called the two readings byte-indistinguishable, which is not
quite right and is worth stating precisely, because it changes what the fix has
to protect. What is genuinely identical is the three signals a caller reads to
decide *did anything change*: ``pulled``, ``errors`` and the exit code. Measured
live 2026-08-28 (alpha, cc-08, own-cloud), same box, one turn::

    --only handoff.yaml      -> pulled=0 in_sync=1 scanned=1 errors=0 rc=0
    --only no-such-file.yaml -> pulled=0 in_sync=0 scanned=0 errors=0 rc=0

So a caller that force-freshes a file and diffs it concludes UNCHANGED off a
scan that never opened it. That is exactly the error the incident reporter made
(foxtrot, 2026-08-11): a VERDICT line asserting a file was current, written off
a scan that never opened it, then retracted.

THE DISCRIMINATOR WAS NEVER MISSING. ``pull_continuity`` has always set
``requested_missing`` ("Surfaced, not fatal: a caller naming a non-continuity
file gets a visible signal instead of a silent empty pull"), it is covered by
four assertions in test_owncloud_sync.py, and the endpoint spreads it onto the
wire via ``**stats``. The WRAPPER dropped it: ``requested_missing`` had zero
occurrences in owncloud-pull.sh while ``s3_absent`` had two, in the same grep.
So this is a RENDERING defect, and the tests below pin the renderer.

guard-2018 (an absent field can BE the zero) and guard-3489 (emit the coverage
count beside the result count, and refuse to exit 0 when it is zero) prescribe
the fix. guard-5163 prescribes THIS FILE: a discriminator added in good faith
that happens to take the same value on both branches is decoration, and only a
two-fixture vacuity test separates the two. That is
``test_the_two_cases_do_not_collapse_to_one_answer`` below; it is the assertion
that fails if the coverage line is ever made unconditional or the exit codes are
ever flattened.

The tests drive the SHIPPED renderer, extracted off disk at test time and fed on
STDIN exactly as ``$PYLAUNCH -`` feeds it in production (guard-920: replicate the
production invocation shape, not the contract-ideal one). Extraction is anchored
on stable content markers, not line numbers.

WHAT THIS FILE DOES NOT COVER, stated rather than left implicit (guard-1462):
the seam is the daemon RESPONSE, so everything upstream of it —
``pull_continuity``'s own matching of names against the continuity set, the
endpoint's ``**stats`` spread, and the HTTP round trip — is structurally
unfalsifiable here. Those are covered by test_owncloud_sync.py's four
``requested_missing`` assertions, and by a live two-case run recorded in the
goal's closure evidence. A green run of this file says the renderer reads the
field correctly, not that the field arrives.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from _bash_helpers import BASH  # : never a bare "bash" argv[0]

SCRIPT_DIR = Path(__file__).resolve().parent
PULL_SCRIPT = SCRIPT_DIR.parent / "owncloud-pull.sh"

# The two heredocs, in source order: the FLEET per-agent printer, then the
# SINGLE-AGENT summary printer.
FLEET_PRINTER, SINGLE_PRINTER = 0, 1


def _printer(which: int) -> str:
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PYEOF'\n(.*?)\nPYEOF", src, re.S)
    assert len(blocks) == 2, (
        f"expected exactly 2 PYEOF heredocs in owncloud-pull.sh, found {len(blocks)}"
    )
    return blocks[which]


def _render(which: int, response: str):
    """Run the shipped printer on a canned RESPONSE, the way bash runs it."""
    return subprocess.run(
        [sys.executable, "-"], input=_printer(which),
        env={"RESPONSE": response, "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )


# The two fixtures the old counter conflated. Both are REAL shapes, transcribed
# from live daemon responses on cc-08 rather than invented.
NEVER_LOOKED = (
    '{"backend":"own-cloud","ok":true,"agent":"alpha","scanned":0,"pulled":0,'
    '"in_sync":0,"would_pull":0,"s3_absent":0,"local_ahead_skipped":0,'
    '"multipart_deferred":0,"errors":0,"pulled_files":[],'
    '"requested_missing":["no-such-file.yaml"],"only":["no-such-file.yaml"]}'
)
LOOKED_ALL_IN_SYNC = (
    '{"backend":"own-cloud","ok":true,"agent":"alpha","scanned":1,"pulled":0,'
    '"in_sync":1,"would_pull":0,"s3_absent":0,"local_ahead_skipped":0,'
    '"multipart_deferred":0,"errors":0,"pulled_files":[],"only":["handoff.yaml"]}'
)
NO_SCOPE = (
    '{"backend":"own-cloud","ok":true,"agent":"alpha","scanned":21,"pulled":2,'
    '"in_sync":8,"would_pull":0,"s3_absent":3,"local_ahead_skipped":8,'
    '"multipart_deferred":0,"errors":0,"pulled_files":["goal-reads.jsonl"]}'
)


# --- the renderer must SAY what it did not match -----------------------------

def test_an_unmatched_scope_names_the_names_it_did_not_match():
    r = _render(SINGLE_PRINTER, NEVER_LOOKED)
    assert "no-such-file.yaml" in r.stdout, (
        "the wrapper must name the unmatched scope; the daemon has always sent "
        f"it as requested_missing. got: {r.stdout!r}"
    )
    assert "0/1" in r.stdout, f"coverage must be reported as matched/requested: {r.stdout!r}"


def test_an_unmatched_scope_refuses_to_exit_zero():
    """guard-3489: refuse to exit 0 when the coverage count is zero."""
    r = _render(SINGLE_PRINTER, NEVER_LOOKED)
    assert r.returncode == 4, (
        "a scope that examined nothing must not report success; 4 is the "
        f"wrapper's vacuous-scope code. got rc={r.returncode}: {r.stdout!r}"
    )


def test_the_unmatched_message_says_what_the_zero_MEANS():
    """A count alone is re-derivable; the sentence is what stops the misread.

    The incident was not "I could not see requested_missing" — it was reading
    pulled=0 as 'unchanged'. So the output has to name that inference and
    refuse it, not merely publish another number.
    """
    r = _render(SINGLE_PRINTER, NEVER_LOOKED)
    assert "never looked" in r.stdout and "in sync" in r.stdout, (
        f"the message must contrast the two readings: {r.stdout!r}"
    )


# --- the matched case must stay quiet (no false positive) --------------------

def test_a_matched_scope_reports_full_coverage_and_exits_zero():
    r = _render(SINGLE_PRINTER, LOOKED_ALL_IN_SYNC)
    assert r.returncode == 0, f"a matched scope is not an error: {r.stdout!r}"
    assert "1/1" in r.stdout
    assert "NOTHING WAS SCANNED" not in r.stdout, (
        "the alarm must not fire on a scope that was genuinely examined"
    )
    assert "NOT CONTINUITY FILES" not in r.stdout, (
        "nothing was missing, so no missing-names clause should print"
    )


def test_without_the_flag_the_output_is_unchanged():
    """Existing callers pass no --only; their output must not grow a line.

    /open-questions is the only production --only caller, but every /start goes
    through the no-scope path, so a stray line here would land fleet-wide.
    """
    r = _render(SINGLE_PRINTER, NO_SCOPE)
    assert r.returncode == 0
    assert "--only scope" not in r.stdout, (
        f"no scope was requested, so no scope line may print: {r.stdout!r}"
    )
    assert "NOTHING WAS SCANNED" not in r.stdout


# --- the guard-5163 vacuity proof -------------------------------------------

def test_the_two_cases_do_not_collapse_to_one_answer():
    """The discriminator must DIFFER on the two branches the old output merged.

    Both fixtures still carry the identical old signals — pulled=0 and errors=0
    — which is the precondition that made the ambiguity real. If a future edit
    makes the coverage line unconditional, or flattens the exit codes, the new
    field becomes decoration and every reader sees an instrumented counter and
    stops looking. This assertion is the only thing standing between those two
    worlds (guard-5163).
    """
    never = _render(SINGLE_PRINTER, NEVER_LOOKED)
    looked = _render(SINGLE_PRINTER, LOOKED_ALL_IN_SYNC)

    for name, r in (("never-looked", never), ("looked", looked)):
        assert "pulled=0" in r.stdout, f"{name} must still carry the old pulled=0"
        assert "errors=0" in r.stdout, f"{name} must still carry the old errors=0"

    assert never.returncode != looked.returncode, (
        "the exit code must separate 'never looked' from 'looked, nothing to "
        f"pull'; both returned {never.returncode}"
    )
    assert never.stdout != looked.stdout, (
        "the rendered output must separate the two cases"
    )


# --- the bash side: pyrc 4 must become a nonzero process exit ----------------

def _run_exit_case(pyrc: int):
    """Drive the shipped `case $pyrc in` mapping with a chosen printer rc."""
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^case \$pyrc in\n.*?^esac$", src, re.M | re.S)
    assert m, "pyrc case-statement anchor not found"
    harness = f'pyrc={pyrc}\nSUMMARY="summary-line"\nRESPONSE="{{}}"\n' + m.group(0) + "\n"
    h = SCRIPT_DIR / f"_tmp_pullexit_{pyrc}.sh"
    try:
        h.write_text(harness, encoding="utf-8")
        return subprocess.run([BASH, str(h)], capture_output=True, text=True)
    finally:
        h.unlink(missing_ok=True)


def test_bash_turns_a_vacuous_scope_into_a_nonzero_exit():
    r = _run_exit_case(4)
    assert r.returncode == 2, (
        "a vacuous scope is a USAGE fault (the invocation named non-continuity "
        f"files), not a pull error; expected 2, got {r.returncode}"
    )
    assert "nothing was CHECKED" in r.stderr, (
        f"the caller needs the reason on stderr: {r.stderr!r}"
    )


def test_the_existing_exit_mappings_are_untouched():
    """Positive control for the case above: 0 and 2 must behave as before."""
    assert _run_exit_case(0).returncode == 0
    assert _run_exit_case(2).returncode == 1


# --- fleet mode ---------------------------------------------------------------

def test_fleet_printer_also_names_an_unmatched_scope():
    r = _render(FLEET_PRINTER, NEVER_LOOKED)
    assert r.returncode == 4, f"fleet must flag it too: rc={r.returncode} {r.stdout!r}"
    assert "no-such-file.yaml" in r.stdout


def test_fleet_printer_stays_quiet_on_a_matched_scope():
    r = _render(FLEET_PRINTER, LOOKED_ALL_IN_SYNC)
    assert r.returncode == 0
    assert "NOTHING WAS SCANNED" not in r.stdout


def test_fleet_loop_counts_a_vacuous_scope_as_a_failure():
    """rc 4 from the per-agent printer must reach agents_failed / fleet_rc.

    Without this the sweep prints the alarm and still exits 0, which is the
    same false all-clear one level up.
    """
    src = PULL_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'^\s+case "\$\{_prc:-0\}" in\n.*?^\s+esac$', src, re.M | re.S)
    assert m, "fleet _prc case anchor not found"
    harness = (
        'agents_failed=0\nfleet_rc=0\n_prc=4\n'
        + m.group(0) + "\n"
        + 'echo "failed=$agents_failed rc=$fleet_rc"\n'
    )
    h = SCRIPT_DIR / "_tmp_fleetprc.sh"
    try:
        h.write_text(harness, encoding="utf-8")
        r = subprocess.run([BASH, str(h)], capture_output=True, text=True)
    finally:
        h.unlink(missing_ok=True)
    assert "failed=1 rc=1" in r.stdout, (
        f"a vacuous per-agent scope must count as a failure: {r.stdout!r} {r.stderr!r}"
    )


# --- the boundary between the two: a PARTIAL match ---------------------------

PARTIAL = (
    '{"backend":"own-cloud","ok":true,"agent":"alpha","scanned":1,"pulled":0,'
    '"in_sync":1,"would_pull":0,"s3_absent":0,"local_ahead_skipped":0,'
    '"multipart_deferred":0,"errors":0,"pulled_files":[],'
    '"requested_missing":["bogus.yaml"],'
    '"only":["bogus.yaml","handoff.yaml"]}'
)


def test_a_partial_match_names_the_miss_but_does_not_fail():
    """One real name + one bogus: report the miss, but do NOT refuse.

    This is the boundary the alarm must not overshoot. Something WAS examined,
    so the pull's result is a real measurement and rc must stay 0 — failing
    here would break any caller that deliberately passes a superset of names.
    The missing name still has to be printed, because the caller's mental model
    of what got refreshed is otherwise wrong by one file. Verified live on
    cc-08 2026-08-28: `--only handoff.yaml,bogus.yaml` -> "1/2 ... NOT
    CONTINUITY FILES: bogus.yaml", rc=0.
    """
    r = _render(SINGLE_PRINTER, PARTIAL)
    assert r.returncode == 0, (
        f"a partial scope did real work; it is not a usage failure: {r.stdout!r}"
    )
    assert "1/2" in r.stdout
    assert "bogus.yaml" in r.stdout, "the unmatched name must still be named"
    assert "NOTHING WAS SCANNED" not in r.stdout, (
        "the vacuous-scope alarm must not fire when one name did match"
    )
