"""Tests for self-assess-and-decide.sh confirming-vs-drift discriminator ().

The script weights `self_evolution_signals_count` for the act_later gate. Before
g-115-1680 the weight was the RAW count, so a high count of CONFIRMING signals
(team consensus an agent is on-lane) misread as self-evolution PRESSURE
(fresh-eyes 2026-06-28: evo=5 with 4/5 partner beliefs confirming zeta's lane
wrongly returned act_later). The fix adds `confirming_signal_fraction` (0..1,
default 0.0 = legacy raw-count behavior) and gates on
`effective_evo_count` so the gate fires on net-DIVERGENT signal, not gross
volume.

g-115-9566 made that subtraction an INTEGER one. The fraction is a lossy
encoding of a count -- a caller computes `confirming_beliefs / count`, and at
`net == 2` the float product put the verdict at the mercy of how many decimal
digits of a repeating quotient the caller happened to type (0.6666 and 0.6667
decided opposite ways at evo=6, printing a byte-identical rationale both times).
The helper now takes `confirming_signal_count` directly when the caller has it,
and otherwise recovers the numerator from the fraction by half-up rounding, so
the gate is `count - confirming_count >= 2` between two integers. Where a
fraction names no whole number of signals the recovered count is the nearest
one; that moves the fraction-space flip from `1 - 2.0/count` to `1 - 1.5/count`,
which is why two boundary pins below carry different fractions than they did.
"""
import json
import subprocess
from pathlib import Path

# Canonical bash resolution + path form for core/scripts tests (see
# _bash_helpers.py): BASH is the resolved interpreter; pass script paths via
# .as_posix() so MSYS bash does not mangle Windows backslashes.
from _bash_helpers import BASH

SCRIPT = Path(__file__).resolve().parents[1] / "self-assess-and-decide.sh"


def _decide(signals: dict, review_type: str = "fresh-eyes-review") -> dict:
    """Run the script with a signals envelope on stdin; return the parsed decision."""
    proc = subprocess.run(
        [BASH, SCRIPT.as_posix(), "--review-type", review_type],
        input=json.dumps(signals),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"non-zero exit {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout)


# Baseline: keep all OTHER act_later/act_now triggers below threshold so the
# decision turns purely on the evo-count path.
_QUIET = {"signal_actionable_score": 0.1, "portfolio_drift_score": 0.1,
          "self_last_updated_days": 5}


def test_legacy_no_fraction_field_unchanged():
    """Backward-compat: omitting confirming_signal_fraction keeps raw-count behavior."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 2})
    assert d["decision"] == "act_later"
    assert "evo_signals=2" in d["rationale"]


def test_incident_high_confirming_fraction_downweights_to_no_change():
    """The  incident: evo=5 with 80% confirming -> 4 of 5 -> net 1 < 2."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 0.8})
    assert d["decision"] == "no_change"
    # rationale surfaces both raw and net-divergent counts for auditability
    assert "evo=5" in d["rationale"]
    assert "net=1" in d["rationale"]


def test_pure_divergent_still_triggers_act_later():
    """fraction=0.0 (all divergent / no direction info) keeps the gate firing."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.0})
    assert d["decision"] == "act_later"
    assert "evo_signals=3" in d["rationale"]


def test_all_confirming_zeroes_pressure():
    """fraction=1.0 (pure consensus) -> 5 of 5 -> net 0 -> no self-evolution pressure."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 1.0})
    assert d["decision"] == "no_change"
    assert "net=0" in d["rationale"]


def test_fraction_clamped_above_one():
    """fraction > 1.0 is clamped to 1.0 (no negative effective count / inversion)."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 1.5})
    assert d["decision"] == "no_change"
    assert "net=0" in d["rationale"]


def test_fraction_clamped_below_zero():
    """fraction < 0.0 is clamped to 0.0 (cannot inflate effective above raw)."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 2,
                 "confirming_signal_fraction": -0.5})
    # clamped to 0.0 -> effective = 2 -> act_later (same as legacy raw count)
    assert d["decision"] == "act_later"


def test_partial_confirming_boundary_just_above_threshold():
    """evo=3, fraction=0.3 -> 1 confirming -> net 2 >= 2 -> act_later."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.3})
    assert d["decision"] == "act_later"


def test_partial_confirming_boundary_just_below_threshold():
    """evo=3, fraction=0.5 -> 2 confirming -> net 1 < 2 -> no_change.

    The pinned fraction is 0.5 rather than the 0.4 this test carried before
    g-115-9566, and the move is the fix rather than a regression: the flip in
    fraction space is `1 - 1.5/N` under integer counts (0.5 at N=3), where it
    was `1 - 2.0/N` (0.333) under the float product. Measured against the live
    helper at N=3: 0.49 -> act_later, 0.50 -> no_change.
    """
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.5})
    assert d["decision"] == "no_change"


def test_fraction_naming_no_whole_signal_rounds_to_the_nearest_one():
    """evo=3, fraction=0.4 asks for 1.2 confirming signals, which cannot exist.

    Half-up recovery gives 1, so 2 of the 3 signals are divergent and the gate
    fires. The rationale reports 33% -- the fraction the COUNTS imply -- not the
    0.4 the caller typed, so the text can never disagree with the verdict.
    """
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.4})
    assert d["decision"] == "act_later"
    assert "net-divergent 2 after 33% confirming" in d["rationale"]


def test_confirming_fraction_does_not_block_other_triggers():
    """High confirming fraction must not suppress an INDEPENDENT drift trigger."""
    # drift >= 0.4 is its own act_later trigger, independent of evo count.
    d = _decide({"signal_actionable_score": 0.1, "self_last_updated_days": 5,
                 "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 1.0,
                 "portfolio_drift_score": 0.5})
    assert d["decision"] == "act_later"
    assert "drift=0.50" in d["rationale"]


# --- integer confirming_signal_count path () ----------------------


def test_confirming_signal_count_is_authoritative_when_supplied():
    """The count is used as given -- no fraction is consulted and none is needed."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 6,
                 "confirming_signal_count": 4})
    assert d["decision"] == "act_later"
    assert "net-divergent 2 after 67% confirming" in d["rationale"]

    d = _decide({**_QUIET, "self_evolution_signals_count": 6,
                 "confirming_signal_count": 5})
    assert d["decision"] == "no_change"
    assert "net=1" in d["rationale"]


def test_confirming_signal_count_outranks_a_disagreeing_fraction():
    """Both supplied and inconsistent: the count wins, because it cannot be lossy."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 6,
                 "confirming_signal_count": 5,
                 "confirming_signal_fraction": 0.0})
    assert d["decision"] == "no_change"
    assert "net=1" in d["rationale"]


def test_typed_precision_cannot_decide_the_verdict():
    """The knife-edge  removed: evo=6 at the true quotient 4/6.

    Under the float product these four spellings of the same number decided
    act_later / no_change / no_change / act_later while printing a
    byte-identical `net=2.0 @67%conf` every time. They now agree, and they agree
    with the P>=2 derivation in core/config/rationale/fresh-eyes-self-assess-axes.md
    (2 never-confirming signals force act_later however the beliefs classify).
    """
    for typed in ("0.6666", "0.6667", "0.666667", "0.6666666666666666"):
        d = _decide({**_QUIET, "self_evolution_signals_count": 6,
                     "confirming_signal_fraction": float(typed)})
        assert d["decision"] == "act_later", f"{typed} -> {d['decision']}"
        assert "net-divergent 2 after 67% confirming" in d["rationale"]


def test_exact_integer_ratio_recovers_its_own_numerator():
    """Every fraction a real caller can emit is k/n, and half-up returns k.

    Real callers divide two integers (confirming_beliefs / signal count), so
    recovery is exact for every input the spec can produce -- the flip-point
    move above is confined to fractions that name no whole signal.
    """
    for n in range(1, 13):
        for k in range(0, n + 1):
            by_fraction = _decide({**_QUIET, "self_evolution_signals_count": n,
                                   "confirming_signal_fraction": k / n})
            by_count = _decide({**_QUIET, "self_evolution_signals_count": n,
                                "confirming_signal_count": k})
            assert by_fraction["decision"] == by_count["decision"], f"n={n} k={k}"
            assert by_fraction["rationale"] == by_count["rationale"], f"n={n} k={k}"
            expected = "act_later" if n - k >= 2 else "no_change"
            assert by_count["decision"] == expected, f"n={n} k={k}"


def test_confirming_signal_count_clamped_to_the_signal_count():
    """More confirming than counted signals cannot drive net below zero."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_count": 9})
    assert d["decision"] == "no_change"
    assert "net=0" in d["rationale"]


def test_negative_confirming_signal_count_clamped_to_zero():
    """A negative count cannot inflate net above the raw signal count."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 2,
                 "confirming_signal_count": -5})
    assert d["decision"] == "act_later"
    assert "evo_signals=2" in d["rationale"]
