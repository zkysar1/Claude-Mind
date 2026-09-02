"""Pin the idempotent EXIT-trap finalize of the  counter block ().

The defect: a SIGTERM landing after the phase dispatch but before the end of
`recurring-close.sh` dropped the seven-field counter block entirely. verify had
already advanced `achievedCount` / `lastAchievedAt`, so the close was committed
on the record while `consecutive_routine`, `consecutive_deep`,
`substantive_hits`, `substantive_runs`, `last_outcome_origin` and `pull_signal`
never moved. Nothing could repair it afterwards: the block is not reachable by
any `--phase` call, and it reads post-phase state by construction, so the
move-the-write remedy g-115-4138 used for the Phase-6 sentinel (bb3e67c02) does
not transfer.

The fix wraps the block in `finalize_counters()`, calls it from the normal path
AND from an EXIT trap installed before the dispatch. Two properties carry the
whole design and each has a test below:

  * IDEMPOTENT -- the block is read-modify-write (`current + 1`), so a blind
    re-run from the trap would double-count every counter (guard-1185).
    `COUNTERS_FINALIZED` makes it one-shot per process.
  * NO BEHAVIOUR CHANGE ON A COMPLETED RUN -- `COUNTERS_OWED` has two setters.
    Gating only on "verify landed" would have stopped a verify-FAILED close from
    advancing its counters, which is a path this goal never set out to touch.

The bash-level tests SPLICE THE REAL TEXT out of `recurring-close.sh` (the flag
block, the trap function + its three `trap` lines, and the function header with
its two guards) and run it against a stub body. A test that reproduced those
lines instead would keep passing after someone inverted the guard in production
-- which is the failure this file exists to catch.

Cross-refs: g-115-8668 (this fix), g-115-4138 + guard-2592 (the sentinel half of
the same window, and the `timeout N` delivery vector), guard-1185 (double-count),
g-115-898 / test_recurring_close_outcome_origin.py (the genuine/forced-flip split
this must preserve), g-115-7136 (`deep_write_landed` narration gate).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from _bash_helpers import BASH as BASH_PATH  # rb-1472: bin-first, clean-PATH-safe

SCRIPT = Path(__file__).resolve().parents[1] / "recurring-close.sh"


def _lines() -> list[str]:
    return SCRIPT.read_text(encoding="utf-8").split("\n")


def _slice(first: str, last: str, label: str) -> str:
    """Return the real text from `first` through `last`, inclusive.

    Both anchors must match exactly one line. An anchor that has drifted fails
    the test loudly rather than silently splicing a wrong range -- the
    hand-rolled-extraction hazard guard-2222 names.
    """
    lines = _lines()
    starts = [i for i, l in enumerate(lines) if l == first]
    ends = [i for i, l in enumerate(lines) if l == last]
    assert len(starts) == 1, f"{label}: opening anchor matched {len(starts)} lines, want 1"
    assert len(ends) == 1, f"{label}: closing anchor matched {len(ends)} lines, want 1"
    assert starts[0] < ends[0], f"{label}: anchors are out of order"
    return "\n".join(lines[starts[0]:ends[0] + 1])


def _function_span(name: str) -> tuple[int, int]:
    """(open, close) 0-based line indices of a column-0 bash function."""
    lines = _lines()
    opens = [i for i, l in enumerate(lines) if l == f"{name}() {{"]
    assert len(opens) == 1, f"{name}(): found {len(opens)} definitions, want 1"
    start = opens[0]
    close = next((i for i, l in enumerate(lines) if i > start and l == "}"), None)
    assert close is not None, f"{name}(): no column-0 closing brace found"
    return start, close


# ─── bash harness: real control flow, stub body ─────────────────────────────


def _run_harness(tmp_path: Path, *, owed: bool, sigterm: bool):
    """Run the REAL guard/trap text with a stub counter body.

    The stub appends one line per execution of the block, so the count of lines
    in the marker file IS the number of times the counters would have advanced.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "fired.log"
    flags = _slice("COUNTERS_OWED=0", "PY_RC=0", "flag block")
    trap_block = _slice("_recurring_close_on_exit() {", "trap 'exit 130' INT", "trap block")
    guards = _slice("finalize_counters() {", "    COUNTERS_FINALIZED=1", "guard block")

    script = f"""
set -uo pipefail
FAILED_PHASE=""
MARKER="{marker}"

{flags}

{trap_block}

{guards}
    printf 'fired\\n' >> "$MARKER"
}}

# ── simulated phase dispatch ──
COUNTERS_OWED={1 if owed else 0}
{'kill -TERM $$' if sigterm else ''}
finalize_counters
printf 'reached-end\\n' >> "$MARKER"
"""
    proc = subprocess.run(
        [BASH_PATH, "-c", script], capture_output=True, text=True, timeout=30
    )
    fired = marker.read_text(encoding="utf-8").split("\n") if marker.exists() else []
    return proc, [l for l in fired if l]


def test_sigterm_after_phases_still_finalizes_the_counters(tmp_path):
    """C1 -- the defect itself. A kill in the post-dispatch window must still
    land the counter block instead of dropping all seven fields."""
    proc, fired = _run_harness(tmp_path, owed=True, sigterm=True)
    assert fired.count("fired") == 1, (
        "a SIGTERM after the phase dispatch must still finalize the counters "
        f"exactly once; block ran {fired.count('fired')} time(s). This is the "
        "g-115-8668 defect: achievedCount advances and the counters do not."
    )
    assert "reached-end" not in fired, (
        "the harness did not actually abort -- the normal path ran to completion, "
        "so this test proved nothing about the trap"
    )
    assert proc.returncode == 143, (
        f"expected SIGTERM exit 143, got {proc.returncode}; the explicit TERM trap "
        "is what makes EXIT-trap delivery deterministic under a signal"
    )


def test_normal_close_finalizes_exactly_once(tmp_path):
    """C2 -- the EXIT trap must be a no-op after the normal call. A second
    advance would double-count every read-modify-write field (guard-1185)."""
    proc, fired = _run_harness(tmp_path, owed=True, sigterm=False)
    assert fired.count("fired") == 1, (
        f"an uninterrupted close ran the counter block {fired.count('fired')} "
        "times; COUNTERS_FINALIZED must make the EXIT trap's call a no-op"
    )
    assert "reached-end" in fired
    assert proc.returncode == 0


def test_two_sequential_closes_advance_once_each(tmp_path):
    """C2 -- across processes: each close advances exactly one step, so N
    closes advance the counters by N and not by 2N."""
    fired_total = []
    for _ in range(2):
        _, fired = _run_harness(tmp_path, owed=True, sigterm=False)
        fired_total = fired
    assert fired_total.count("fired") == 2, (
        f"two closes produced {fired_total.count('fired')} advances, want 2"
    )


def test_a_verify_failure_still_leaves_the_counters_owed(tmp_path):
    """THE DISCRIMINATING ROW (guard-2353).

    This guard narrowed a predicate: the counter block used to run
    unconditionally after the dispatch, and now runs behind COUNTERS_OWED. A
    green pre-existing suite is NOT evidence for that change unless some fixture
    lies on the FAR SIDE of the old boundary -- and none did, because no test in
    the recurring-close family exercises a close whose verify FAILED.

    That is the one path where the two candidate designs disagree. Under a
    verify-only gate the counters would silently stop advancing on a
    verify-failed close (a behaviour change this goal never intended); under the
    shipped two-setter design they still advance, exactly as before.

    Runs the REAL run_phase against a stub iteration-close.sh that fails, then
    the REAL post-dispatch setter line.
    """
    lines = _lines()
    stub_dir = tmp_path / "scripts"
    stub_dir.mkdir()
    (stub_dir / "iteration-close.sh").write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")

    flags = _slice("COUNTERS_OWED=0", "PY_RC=0", "flag block")
    # run_phase, verbatim, including the verify-ok setter that must NOT fire here
    starts = [i for i, l in enumerate(lines) if l == "run_phase() {"]
    assert len(starts) == 1, "run_phase() anchor drifted"
    close = next(i for i, l in enumerate(lines) if i > starts[0] and l == "}")
    run_phase = "\n".join(lines[starts[0]:close + 1])
    # the post-dispatch setter, verbatim
    setter = [i for i, l in enumerate(lines) if l == "COUNTERS_OWED=1"]
    assert len(setter) == 1, (
        f"expected exactly 1 unconditional post-dispatch COUNTERS_OWED=1, found {len(setter)}"
    )

    script = f"""
set -uo pipefail
SCRIPT_DIR="{stub_dir}"
MAX_RC=0
PHASE_RESULTS=""
FAILED_PHASES=""
FAILED_RETRY_CMDS=""
FAILED_PHASE=""
{flags}

{run_phase}

run_phase verify --phase verify
# verify FAILED, so the verify-ok setter did not fire:
echo "after-verify COUNTERS_OWED=$COUNTERS_OWED"
{lines[setter[0]]}
echo "after-dispatch COUNTERS_OWED=$COUNTERS_OWED"
echo "PHASE_RESULTS=$PHASE_RESULTS"
"""
    proc = subprocess.run(
        [BASH_PATH, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert "verify=fail(3)" in proc.stdout, (
        f"harness did not actually fail verify; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "after-verify COUNTERS_OWED=0" in proc.stdout, (
        "the verify-ok setter fired on a FAILED verify -- the discriminating row "
        f"proves nothing if both setters fire. stdout={proc.stdout!r}"
    )
    assert "after-dispatch COUNTERS_OWED=1" in proc.stdout, (
        "a verify-FAILED close no longer marks the counters owed. Before this "
        "change the block ran unconditionally after the dispatch, so this is a "
        "silent behaviour change on a path the fix never intended to touch "
        f"(guard-1080). stdout={proc.stdout!r}"
    )


def test_abort_before_the_counters_are_owed_does_not_finalize(tmp_path):
    """A run that died before the close committed must NOT advance anything --
    the trap is a repair path, not an unconditional writer."""
    proc, fired = _run_harness(tmp_path, owed=False, sigterm=True)
    assert fired.count("fired") == 0, (
        "the counter block fired on a run that never committed a close; "
        "COUNTERS_OWED is what keeps the trap from inventing an advance"
    )
    assert proc.returncode == 143


def test_abort_path_finalize_is_distinguishable_in_the_log(tmp_path):
    """The rescue must be OBSERVABLE, or it is unfalsifiable in the field.

    finalize_counters prints identical narration on both paths, so without a
    dedicated marker no log distinguishes "the trap saved this close" from
    "nothing was ever interrupted" -- and the recurrence this fix exists to stop
    could never be attributed either way. The marker must fire on the rescue and
    must NOT fire on a normal close, or it is just noise.
    """
    proc_abort, _ = _run_harness(tmp_path / "a", owed=True, sigterm=True)
    assert "ABORT-PATH FINALIZE" in proc_abort.stderr, (
        "the abort-path rescue is invisible in the log; nobody can measure "
        f"whether the trap ever fires. stderr={proc_abort.stderr!r}"
    )
    proc_normal, _ = _run_harness(tmp_path / "b", owed=True, sigterm=False)
    assert "ABORT-PATH FINALIZE" not in proc_normal.stderr, (
        "the marker fired on an uninterrupted close, which makes it useless as a "
        f"rescue signal. stderr={proc_normal.stderr!r}"
    )
    proc_notowed, _ = _run_harness(tmp_path / "c", owed=False, sigterm=True)
    assert "ABORT-PATH FINALIZE" not in proc_notowed.stderr, (
        "the marker fired on an abort where no close had committed -- nothing "
        f"was rescued, so claiming a rescue is wrong. stderr={proc_notowed.stderr!r}"
    )


def test_replacement_trap_still_reports_the_failed_phase(tmp_path):
    """Bash allows one trap per signal, so this trap REPLACED the
    FAILED_PHASE-only trap at the top of the file. It must keep that job."""
    trap_block = _slice("_recurring_close_on_exit() {", "trap 'exit 130' INT", "trap block")
    script = f"""
set -uo pipefail
FAILED_PHASE="state-update"
{trap_block}
exit 7
"""
    proc = subprocess.run(
        [BASH_PATH, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert "ABORT during: state-update" in proc.stderr, (
        "the replacement EXIT trap dropped the ABORT diagnostic the original "
        f"trap emitted; stderr was: {proc.stderr!r}"
    )
    assert proc.returncode == 7, (
        f"the EXIT trap must preserve the original exit status, got {proc.returncode}"
    )


def test_trap_tolerates_finalize_not_yet_defined(tmp_path):
    """The trap is installed BEFORE `finalize_counters` is defined, so it can
    fire against an undefined function. The `declare -F` guard keeps that from
    printing a spurious 'command not found' over the real diagnostic."""
    trap_block = _slice("_recurring_close_on_exit() {", "trap 'exit 130' INT", "trap block")
    script = f"""
set -uo pipefail
FAILED_PHASE=""
{trap_block}
exit 0
"""
    proc = subprocess.run(
        [BASH_PATH, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert "command not found" not in proc.stderr, (
        f"trap emitted a spurious error when finalize_counters was undefined: {proc.stderr!r}"
    )
    assert proc.returncode == 0


# ─── structural: nothing escapes the guard ──────────────────────────────────


def test_trap_is_installed_before_the_phase_dispatch():
    """Installing the net after the dispatch would leave the state-update /
    learning-gate / productivity window uncovered -- exactly where the
    achievedCount-vs-counters split is already open."""
    lines = _lines()
    install = next(i for i, l in enumerate(lines) if l == "trap _recurring_close_on_exit EXIT")
    first_phase = next(i for i, l in enumerate(lines) if l.startswith("run_phase "))
    assert install < first_phase, (
        f"the EXIT trap is installed at line {install + 1}, after the first "
        f"run_phase dispatch at line {first_phase + 1}. The net must cover the "
        "whole dispatch, not just its tail."
    )


def test_every_counter_write_site_lives_inside_the_guarded_function():
    """O2/O4 -- a write left outside the function would run unguarded on the
    normal path and be lost on the abort path: the worst of both."""
    lines = _lines()
    start, close = _function_span("finalize_counters")
    outside = [
        i + 1
        for i, l in enumerate(lines)
        if '"update-goal"' in l and not (start < i < close)
    ]
    assert not outside, (
        f"aspirations.py update-goal write site(s) outside finalize_counters at "
        f"line(s) {outside}. Every counter write must be inside the guard."
    )
    inside = sum(1 for i, l in enumerate(lines) if '"update-goal"' in l and start < i < close)
    assert inside >= 6, (
        f"only {inside} update-goal write sites found inside finalize_counters; "
        "the g-317-02 block writes consecutive_routine, consecutive_deep, "
        "last_outcome_origin, substantive_runs, substantive_hits and pull_signal"
    )


@pytest.mark.parametrize(
    "needle",
    [
        'if [[ "$OUTCOME" == "deep" && "$ORIGINAL_OUTCOME" == "routine" ]]; then',
        'OUTCOME_ORIGIN="forced-flip"',
        'OUTCOME_ORIGIN="genuine"',
    ],
)
def test_forced_flip_derivation_is_inside_the_guarded_function(needle):
    """C3/O3 -- the genuine-vs-forced-flip split is derived from post-phase
    state, so it must move INTO the function with the block it feeds. Left
    outside, the abort path would finalize with a stale or unset origin and a
    forced flip would advance `substantive_hits` / `consecutive_deep` after all."""
    lines = _lines()
    start, close = _function_span("finalize_counters")
    hits = [i for i, l in enumerate(lines) if needle in l]
    assert hits, f"derivation anchor vanished: {needle!r}"
    assert all(start < i < close for i in hits), (
        f"{needle!r} appears outside finalize_counters (lines "
        f"{[i + 1 for i in hits if not (start < i < close)]})"
    )


def test_pull_signal_clear_stays_gated_on_has_pull_signal():
    """O4 -- the clear must fire only when the goal CARRIED a signal. An
    unconditional write would stamp `pull_signal: null` onto every recurring
    record in the store."""
    lines = _lines()
    start, close = _function_span("finalize_counters")
    body = "\n".join(lines[start:close + 1])
    assert "if has_pull_signal:" in body, (
        "the has_pull_signal gate is gone -- clearing unconditionally writes "
        "pull_signal: null onto every recurring goal, not just the ones that "
        "carried a signal"
    )
    assert '"pull_signal", "null"' in body or "pull_signal" in body


def test_py_rc_is_preseeded_so_an_early_return_cannot_trip_set_u():
    """`PY_RC` is consumed at the bottom of the script. finalize_counters can
    return before assigning it, and the script runs under `set -u`."""
    lines = _lines()
    seed = [i for i, l in enumerate(lines) if l == "PY_RC=0"]
    assign = [i for i, l in enumerate(lines) if l == "PY_RC=$?"]
    consume = [i for i, l in enumerate(lines) if "$PY_RC" in l and "if" in l]
    assert seed, "PY_RC is never pre-seeded; an early return would trip `set -u`"
    assert assign and consume, "PY_RC assign/consume anchors drifted"
    assert seed[0] < assign[0] < consume[0]
