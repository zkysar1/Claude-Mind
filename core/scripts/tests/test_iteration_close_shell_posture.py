"""`iteration-close.sh` keeps `set -e` ON PURPOSE, and the abort is made loud by an
EXIT trap. Pin all three halves so none of them can silently revert (g-115-9659).

WHY THIS EXISTS
---------------
guard-614 says a wrapper that must emit on every exit path should use
`set -uo pipefail`, not `set -e`. Read alone, that reads as a standing defect in
this file, and it was filed as one. It is not: `do_verify`'s status write
(`"${update_cmd[@]}"`) is invoked BARE *precisely so a refused close is fatal*.
Under `set -uo pipefail` the refusal would print and the function would carry on
into the outcome_class / completed_by_role stamps, the `Completed:` COORDINATION
BOARD POST and the team-state in_flight clear — telling the reducer and every
partner Body that a goal closed when it did not. A loud abort beats a false
completion, so the `-e` stays and the reconciliation is written at the `set` line.

The complement is that the abort is NOT silent: the EXIT trap registered
immediately before the dispatch `case` calls `_print_recovery_instructions`,
which names the rc, the phase and the goal on stderr and probes live state. That
wiring lives ~4.6k lines from the `set` line, which is why a reader (and the goal
that produced this file) concludes "silent" from the posture alone.

WHAT IS NOT DUPLICATED HERE
---------------------------
`test_post_status_stamps_are_non_fatal.py` already pins that every write ordered
AFTER the status write carries a `||` fallback, and that the status write itself
does not. That test cannot catch the regression THIS file exists for: flipping
line `set -euo pipefail` to `set -uo pipefail` adds no `||` anywhere, so every
assertion over there stays green while the status write stops being fatal.
(That file is RED at time of writing — g-358-36 folded the completed_date stamp
into the status write and its NON_FATAL_STAMPS tuple was never re-anchored, so it
fails on the first field and never reaches the two live ones. Tracked by
g-115-8624; deliberately not fixed here, it has an owner.)

Run:
  STORAGE_BACKEND=local py -3 -m pytest core/scripts/tests/test_iteration_close_shell_posture.py -q
"""

from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
CLOSE_SH = CORE_ROOT / "scripts" / "iteration-close.sh"

# The marker the reconciliation block must keep. Short and stable on purpose —
# pinning prose would fail on every copy-edit; pinning nothing lets the
# justification be deleted while the posture it explains stays behind.
RECONCILIATION_MARKER = "GUARD-614 RECONCILIATION"


def _lines():
    return CLOSE_SH.read_text(encoding="utf-8").splitlines()


def _index_of(pred, what):
    lines = _lines()
    hits = [i for i, ln in enumerate(lines) if pred(ln)]
    assert hits, f"could not locate {what} in iteration-close.sh — re-anchor this test, do not delete it"
    assert len(hits) == 1, f"expected exactly one {what}, found {len(hits)} at lines {[h + 1 for h in hits]}"
    return hits[0], lines


def test_posture_is_errexit():
    """The one-line regression: `set -e` must not be dropped from the posture.

    Dropping it makes do_verify's bare status write non-fatal, so a REFUSED close
    continues into the stamps and the coordination board post — announcing a
    completion that never landed.
    """
    idx, lines = _index_of(lambda ln: ln.startswith("set "), "top-level `set` line")
    assert lines[idx] == "set -euo pipefail", (
        f"iteration-close.sh's shell posture is now `{lines[idx]}` (line {idx + 1}). "
        f"`set -e` is deliberate: it is the ONLY thing that stops do_verify after a "
        f"refused status write. Without it the close continues into the outcome_class "
        f"stamp, the `Completed:` board post and the in_flight clear, reporting a "
        f"completion that did not happen. If this is an intentional redesign, the "
        f"status write must become explicitly checked FIRST (g-115-9659)"
    )


def test_exit_trap_wires_recovery_and_precedes_the_dispatch():
    """The `made loud` half. A `set -e` abort is only non-silent because of this trap.

    Ordering is load-bearing: a trap registered AFTER the dispatch never arms for
    the phase it is meant to report on.
    """
    trap_idx, lines = _index_of(lambda ln: ln.startswith("trap "), "EXIT trap registration")
    trap = lines[trap_idx]
    assert "_print_recovery_instructions" in trap, (
        "the EXIT trap no longer calls _print_recovery_instructions. That call is the "
        "only reason a `set -e` abort inside do_verify names itself on stderr instead "
        "of exiting mute (g-115-9659 outcome 2)"
    )
    assert trap.rstrip().endswith("EXIT"), f"the trap is no longer registered on EXIT: {trap}"

    dispatch = [
        i for i, ln in enumerate(lines)
        if ln.startswith('case "$PHASE" in') and "do_verify" in "\n".join(lines[i:i + 10])
    ]
    assert dispatch, "could not locate the dispatch `case` that calls do_verify"
    assert trap_idx < dispatch[0], (
        f"the EXIT trap (line {trap_idx + 1}) is registered AFTER the dispatch "
        f"(line {dispatch[0] + 1}). A phase that aborts before the trap arms exits "
        f"with no RECOVERY block at all — the silent close this pin exists to prevent"
    )


def test_posture_carries_the_guard_614_reconciliation():
    """The justification must stay CO-LOCATED with the flags it explains.

    It was not, and the cost is measured: a reader who found only
    `set -euo pipefail` filed g-115-9659 against a defect that had already been
    fixed, prescribing a remedy that would have regressed the file.
    """
    lines = _lines()
    set_idx = next(i for i, ln in enumerate(lines) if ln.startswith("set "))
    header = "\n".join(lines[:set_idx])
    assert RECONCILIATION_MARKER in header, (
        f"the `{RECONCILIATION_MARKER}` block above the `set` line is gone. Without it "
        f"the next reader sees `set -euo pipefail` beside guard-614 and re-derives a "
        f"defect that is not there (g-115-9659 outcome 1)"
    )
    assert "guard-614" in header, "the reconciliation no longer names guard-614 by id, so retrieval cannot reach it"
