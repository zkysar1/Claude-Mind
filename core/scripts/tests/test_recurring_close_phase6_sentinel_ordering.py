"""Pin the Phase-6 sentinel write ORDER in recurring-close.sh ().

The defect these tests exist for is invisible to every behavioural test that
already covers this slot: the sentinel was written correctly, just ~836 lines
too late. A SIGTERM at the harness's 2-minute Bash ceiling anywhere between the
phase dispatch and the end of the script left the close committed (or partially
committed) with the sentinel never written, so the stdout imperative AND its
backstop were lost to one event and Phase 6 was silently skipped on a DEEP
close. Four detected instances in 11 days across three agents and three boxes,
every one caught by hand -- so four is a floor, not a rate.

These assertions are STRUCTURAL on purpose. The whole failure mode is positional,
and the full suite was green throughout the eleven days the defect was live, so a
behavioural assertion cannot express it.
"""
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "recurring-close.sh"


def _lines():
    return SCRIPT.read_text(encoding="utf-8").split("\n")


def _index_of(pred, why):
    for i, line in enumerate(_lines()):
        if pred(line):
            return i
    raise AssertionError(why)


def test_sentinel_write_precedes_the_phase_dispatch():
    """The write must land BEFORE the kill window, not after it."""
    call = _index_of(
        lambda l: l.strip() == "write_phase6_sentinel",
        "write_phase6_sentinel is never CALLED -- the sentinel is unreachable",
    )
    first_phase = _index_of(
        lambda l: l.startswith("run_phase "),
        "no `run_phase` dispatch found -- anchor drifted, re-derive this test",
    )
    assert call < first_phase, (
        "The Phase-6 sentinel write (line %d) must precede the first run_phase "
        "dispatch (line %d). Writing it after the phases re-opens the g-115-4138 "
        "window: a SIGTERM in between loses the close's Phase-6 dispatch signal "
        "silently." % (call + 1, first_phase + 1)
    )


def test_sentinel_write_follows_the_outcome_flip():
    """It must still be AFTER Block A/C, or the payload carries a stale outcome."""
    flip = _index_of(
        lambda l: l.strip() == 'OUTCOME="$FINAL_OUTCOME"',
        "Block A/C outcome finalization anchor not found",
    )
    call = _index_of(lambda l: l.strip() == "write_phase6_sentinel", "no call site")
    assert flip < call, (
        "The sentinel write (line %d) must come AFTER OUTCOME is finalized "
        "(line %d) -- the payload's routine-vs-deep field is set by the Block "
        "A/C reclassification and would otherwise be pre-flip." % (call + 1, flip + 1)
    )


def test_exactly_one_wm_set_write_site_for_the_slot():
    """A second write site would be a guard-2104 loss-bearing overwrite."""
    body = SCRIPT.read_text(encoding="utf-8")
    writes = re.findall(r"wm-set\.sh\"?\s+pending_phase_6_spark", body)
    assert len(writes) == 1, (
        "expected exactly 1 wm-set write site for pending_phase_6_spark, found "
        "%d. This slot is a payload-carrying SINGLE-SLOT sentinel (guard-2104): "
        "a second unconditional write silently cancels an unconsumed obligation "
        "and nothing reports the loss." % len(writes)
    )


def test_write_is_guarded_against_clobbering_another_goals_obligation():
    """guard-2104 sanctions MERGE or REFUSE LOUDLY -- this writer refuses."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert 'wm-read.sh" pending_phase_6_spark' in body, (
        "the writer must READ the slot before writing it -- without the read "
        "there is no way to detect an unconsumed obligation (guard-2104)"
    )
    assert "REFUSING to overwrite pending_phase_6_spark" in body, (
        "the writer must refuse LOUDLY on a foreign unconsumed payload; a silent "
        "skip is the same invisibility the refuse-guard exists to remove"
    )
    assert "guard-2104" in body, "the refuse-guard must cite its guardrail"
