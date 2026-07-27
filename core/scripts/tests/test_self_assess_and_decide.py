"""Tests for self-assess-and-decide.sh confirming-vs-drift discriminator ().

The script weights `self_evolution_signals_count` for the act_later gate. Before
g-115-1680 the weight was the RAW count, so a high count of CONFIRMING signals
(team consensus an agent is on-lane) misread as self-evolution PRESSURE
(fresh-eyes 2026-06-28: evo=5 with 4/5 partner beliefs confirming zeta's lane
wrongly returned act_later). The fix adds `confirming_signal_fraction` (0..1,
default 0.0 = legacy raw-count behavior) and gates on
`effective_evo_count = count * (1 - fraction)` so the gate fires on
net-DIVERGENT signal, not gross volume.
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
    """The  incident: evo=5 with 80% confirming -> effective 1.0 < 2."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 0.8})
    assert d["decision"] == "no_change"
    # rationale surfaces both raw and net-divergent counts for auditability
    assert "evo=5" in d["rationale"]
    assert "net=1.0" in d["rationale"]


def test_pure_divergent_still_triggers_act_later():
    """fraction=0.0 (all divergent / no direction info) keeps the gate firing."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.0})
    assert d["decision"] == "act_later"
    assert "evo_signals=3" in d["rationale"]


def test_all_confirming_zeroes_pressure():
    """fraction=1.0 (pure consensus) -> effective 0 -> no self-evolution pressure."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 1.0})
    assert d["decision"] == "no_change"
    assert "net=0.0" in d["rationale"]


def test_fraction_clamped_above_one():
    """fraction > 1.0 is clamped to 1.0 (no negative effective count / inversion)."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 1.5})
    assert d["decision"] == "no_change"
    assert "net=0.0" in d["rationale"]


def test_fraction_clamped_below_zero():
    """fraction < 0.0 is clamped to 0.0 (cannot inflate effective above raw)."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 2,
                 "confirming_signal_fraction": -0.5})
    # clamped to 0.0 -> effective = 2 -> act_later (same as legacy raw count)
    assert d["decision"] == "act_later"


def test_partial_confirming_boundary_just_above_threshold():
    """evo=3, fraction=0.3 -> effective 2.1 >= 2 -> act_later (boundary kept)."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.3})
    assert d["decision"] == "act_later"


def test_partial_confirming_boundary_just_below_threshold():
    """evo=3, fraction=0.4 -> effective 1.8 < 2 -> no_change (boundary kept)."""
    d = _decide({**_QUIET, "self_evolution_signals_count": 3,
                 "confirming_signal_fraction": 0.4})
    assert d["decision"] == "no_change"


def test_confirming_fraction_does_not_block_other_triggers():
    """High confirming fraction must not suppress an INDEPENDENT drift trigger."""
    # drift >= 0.4 is its own act_later trigger, independent of evo count.
    d = _decide({"signal_actionable_score": 0.1, "self_last_updated_days": 5,
                 "self_evolution_signals_count": 5,
                 "confirming_signal_fraction": 1.0,
                 "portfolio_drift_score": 0.5})
    assert d["decision"] == "act_later"
    assert "drift=0.50" in d["rationale"]
