"""Post-status stamps in `iteration-close.sh do_verify` must be NON-FATAL ().

WHY THIS EXISTS
---------------
`do_verify` runs under `set -euo pipefail` (iteration-close.sh L65). The status
write is the ONE call whose failure means "the close did not happen". Every write
ordered after it is bookkeeping or notification — and if any of those is invoked
BARE, a transient lock blip aborts the function after the status has already
landed. The EXIT trap then prints `_print_recovery_instructions`, which offers a
retry (would double-apply the status write) and a revert-to-pending (would UNDO a
completed goal). Neither remedy matches the real state.

What dies with it is not cosmetic: the `Completed:` COORDINATION BOARD POST is
ordered after these stamps, and that post is how the reducer and partner agents
learn a goal closed. Its loss is invisible from the closing box and surfaces as
duplicate work or a stalled handoff on ANOTHER machine.

MEASURED on g-326-627 (cc-08, 2026-08-24T14:43:41): `status=completed` and
`completed_date=2026-08-24` landed; `outcome_class` and `completed_by_role` are
ABSENT; the board carried no post. do_verify died exactly at the `outcome_class`
write, one call past `completed_date`.

Run:
  py -3 -m pytest core/scripts/tests/test_post_status_stamps_are_non_fatal.py -q
"""

import re
import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
CLOSE_SH = CORE_ROOT / "scripts" / "iteration-close.sh"

# Every field stamped AFTER the status write. Each must be non-fatal.
NON_FATAL_STAMPS = ("completed_date", "outcome_class", "completed_by_role")

WRITER = "aspirations-update-goal.sh"
STATUS_WRITE = '"$GOAL_ID" status "$GOAL_STATUS"'


def _do_verify_body() -> str:
    """The text of do_verify, sliced at the next top-level function.

    Sliced rather than fixed-length so these assertions cannot silently start
    reading a neighbouring function as the file grows.
    """
    src = CLOSE_SH.read_text(encoding="utf-8")
    assert "do_verify()" in src, "iteration-close.sh no longer defines do_verify()"
    after = src.split("do_verify()", 1)[1]
    nxt = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\)\s*\{", after)
    return after[: nxt.start()] if nxt else after


def _writer_line_index(lines, field):
    """Index of the line that INVOKES the writer for `field`, or None.

    Requiring the writer on the same line is load-bearing, not belt-and-braces:
    a bare `field in body` check passes against code with no writer at all,
    because the `|| echo "... <field> stamp failed ..."` fallback contains the
    field name. A grep for a name is not a test that something happens.
    """
    for i, ln in enumerate(lines):
        if field in ln and WRITER in ln:
            return i
    return None


def _is_guarded(lines, idx):
    """True when the invocation at `idx` has a `||` fallback.

    Handles both shapes: the guard on the same line, and the guard on the
    continuation line after a trailing backslash.
    """
    line = lines[idx]
    if "||" in line:
        return True
    if line.rstrip().endswith("\\"):
        for follow in lines[idx + 1:]:
            if follow.strip():
                return follow.strip().startswith("||")
    return False


def test_every_post_status_stamp_is_non_fatal():
    """The regression itself. A bare stamp aborts the close after status lands."""
    lines = _do_verify_body().splitlines()
    unguarded = []
    for field in NON_FATAL_STAMPS:
        idx = _writer_line_index(lines, field)
        assert idx is not None, (
            f"do_verify no longer INVOKES the {field} writer via {WRITER} — "
            f"re-anchor this test, do not delete it"
        )
        if not _is_guarded(lines, idx):
            unguarded.append(field)
    assert not unguarded, (
        f"post-status stamp(s) {unguarded} are invoked BARE under `set -euo "
        f"pipefail`. A lock blip there aborts do_verify AFTER the status write "
        f"has landed, killing the coordination board post and the team-state "
        f"in_flight clear, and the EXIT trap then offers a retry that would "
        f"double-apply the status write (g-115-7663, measured on g-326-627)"
    )


def test_the_status_write_itself_stays_FATAL():
    """The complement, and it is not symmetric with the test above.

    A failed status write means the close genuinely did not happen, so rc=1 is
    the CORRECT report (guard-3256 sequence B). Guarding it too would report
    success for a goal that never closed — a strictly worse failure than the one
    this file exists to prevent.
    """
    lines = _do_verify_body().splitlines()
    idx = next((i for i, ln in enumerate(lines) if STATUS_WRITE in ln), None)
    assert idx is not None, (
        "could not locate do_verify's status write — every assertion here is "
        "anchored to it and must be re-anchored, not deleted"
    )
    assert not _is_guarded(lines, idx), (
        "the STATUS write has acquired a `||` fallback. A failed status write "
        "means the close did not happen; swallowing it would report success for "
        "a goal that never closed (guard-3256 sequence B must stay loud)"
    )


def test_stamps_are_ordered_after_the_status_write():
    """Ordering. A stamp before the status write attaches completion provenance
    to a goal that is not yet completed."""
    body = _do_verify_body()
    status_at = body.find(STATUS_WRITE)
    assert status_at != -1, "could not locate the status write to anchor ordering"
    for field in NON_FATAL_STAMPS:
        at = body.find(f'"$GOAL_ID" {field} ')
        assert at != -1, f"could not locate the {field} write"
        assert at > status_at, (
            f"the {field} stamp (offset {at}) precedes the status write "
            f"(offset {status_at})"
        )


def _recovery_verify_branch() -> str:
    """The `verify)` case of _print_recovery_instructions, sliced to its `;;`.

    Sliced rather than grepped over the whole file so these assertions cannot
    pass on text belonging to the sibling `state-update)` branch, which has
    probed live state since g-115-4096 and would satisfy every check below.
    """
    src = CLOSE_SH.read_text(encoding="utf-8")
    assert "_print_recovery_instructions()" in src, (
        "iteration-close.sh no longer defines _print_recovery_instructions()"
    )
    after = src.split("_print_recovery_instructions()", 1)[1]
    at = after.find("\n        verify)\n")
    assert at != -1, "the recovery case no longer has a `verify)` branch"
    rest = after[at:]
    end = rest.find("\n            ;;")
    assert end != -1, "could not find the end of the verify) branch"
    return rest[:end]


def test_verify_recovery_probes_live_state():
    """The verify branch must READ the goal before advising ().

    It previously asserted "may be in indeterminate state" unconditionally — on
    every rc, including the rc=2 entry-check refusal where nothing ran and the
    goal was never touched.
    """
    branch = _recovery_verify_branch()
    assert "_probe_goal_status" in branch, (
        "the verify recovery branch no longer probes live goal status. Without "
        "the probe it cannot tell a close that LANDED from one that never ran, "
        "and it goes back to advising both remedies for every failure "
        "(g-115-7663 outcome 2)"
    )


def test_verify_recovery_revert_line_is_CONDITIONAL():
    """Reverting a goal whose status already landed re-opens a closed goal.

    guard-2760: a destructive remedy needs evidence a reversible one is
    insufficient. Offering it unconditionally is the opposite.
    """
    branch = _recovery_verify_branch()
    revert = [ln for ln in branch.splitlines() if "status pending" in ln and "echo" in ln]
    assert revert, "the verify recovery branch no longer offers a revert at all"
    assert len(revert) == 1, f"expected exactly one revert line, found {len(revert)}"
    indent = len(revert[0]) - len(revert[0].lstrip())
    assert indent > 12, (
        "the revert line sits at top-level branch indent, i.e. it is offered "
        "UNCONDITIONALLY again. It must stay inside the `if` that fires only "
        "when the live status differs from the status this call was writing "
        "(g-115-7663)"
    )


def test_verify_recovery_distinguishes_landed_from_not_landed():
    """All three states must be reachable and say different things."""
    branch = _recovery_verify_branch()
    for probe, why in (
        ("ALREADY on the record", "the LANDED case (interruption after the status write)"),
        ("did NOT land", "the NOT-LANDED case (entry/gate refusal, or the write failing)"),
        ("asserting neither direction", "the UNREADABLE case (fail-open, guard-1091)"),
    ):
        assert probe in branch, (
            f"the verify recovery branch no longer distinguishes {why}; the "
            f"operator cannot tell WHICH half landed (g-115-7663 outcome 2)"
        )


if __name__ == "__main__":
    failures = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"FAIL {name}: {exc}")
    print(f"{'TEST FAIL' if failures else 'TEST PASS'} ({len(failures)} failed)")
    sys.exit(1 if failures else 0)
