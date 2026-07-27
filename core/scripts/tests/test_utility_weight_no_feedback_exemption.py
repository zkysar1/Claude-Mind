"""test_utility_weight_no_feedback_exemption.py — path-c (origin/design ).

Verifies the no-feedback-signal exemption in retrieve.py:_utility_weight: a node
retrieved enough to be measured (rc >= neutral_below) but carrying ZERO feedback
of any kind (times_helpful == times_inferred_helpful == times_noise == 0) returns
neutral 1.0 instead of the 0.5 utility-ratio floor. A node with any times_noise
keeps the penalty (real negative signal). Grounds guard-393 / the reward-layer
feedback-signal asymmetry (system/system-constraints-loop/
agent-feedback-signal-asymmetry-reward-layer.md, Concrete instance).

Pure stdlib. Self-contained — passes an explicit cfg so the live tree.yaml
retrieval config never affects the result.

CENTERING (g-306-95). The weight is `clamp(1.0 + (utility_ratio - center), min,
max)`. CFG below pins `utility_weight_center: 0.5`, which reduces to the
`0.5 + utility_ratio` arithmetic these cases were originally written against —
so every expected value here still asserts exactly what it did before, and the
assumed center is now declared instead of being baked into the implementation.
The live config uses a MEASURED center (the corpus mean utility_ratio), which is
deliberately NOT hardcoded here: this file tests the formula, not the corpus.
Three cases at the bottom cover the centering itself — the last of them pins the
three-way ORDERING (measured-good > unmeasured > measured-bad), which is the
property that was inverted in production and that no isolated value assertion
catches.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from retrieve import _utility_weight  # noqa: E402

CFG = {
    "utility_weight_min": 0.5,
    "utility_weight_max": 1.5,
    "utility_weight_neutral_below_retrievals": 5,
    # center 0.5 => 1.0 + (ur - 0.5) == 0.5 + ur, the pre-centering arithmetic
    # every expected value below was derived against. See module docstring.
    "utility_weight_center": 0.5,
}


def test_underretrieved_returns_neutral():
    # rc below the neutral threshold -> 1.0 (pre-existing behavior, unchanged by path-c).
    assert _utility_weight({"retrieval_count": 2}, CFG) == 1.0


def test_zero_feedback_well_retrieved_is_exempt():
    # rc >= neutral, NO feedback signal of any kind -> exemption returns 1.0.
    # Pre-fix this scored 0.5 + 0.0 -> clamped to the 0.5 floor (an unfair penalty).
    node = {
        "retrieval_count": 10,
        "times_helpful": 0,
        "times_inferred_helpful": 0,
        "times_noise": 0,
        "utility_ratio": 0.0,
    }
    assert _utility_weight(node, CFG) == 1.0


def test_noise_only_keeps_penalty():
    # rc >= neutral WITH real negative signal (times_noise > 0) -> NOT exempt.
    node = {
        "retrieval_count": 10,
        "times_helpful": 0,
        "times_inferred_helpful": 0,
        "times_noise": 3,
        "utility_ratio": 0.0,
    }
    assert _utility_weight(node, CFG) == 0.5  # 0.5 + 0.0 -> floor


def test_helpful_signal_uses_ratio_not_exemption():
    # rc >= neutral WITH positive feedback -> utility_ratio path, not the exemption.
    node = {
        "retrieval_count": 10,
        "times_helpful": 4,
        "times_inferred_helpful": 0,
        "times_noise": 0,
        "utility_ratio": 0.4,
    }
    assert _utility_weight(node, CFG) == 0.9  # 0.5 + 0.4


def test_inferred_helpful_only_uses_ratio_not_exemption():
    # Even the starved positive signal, when present, takes the node off the
    # exemption path (it now has SOME measured signal).
    node = {
        "retrieval_count": 10,
        "times_helpful": 0,
        "times_inferred_helpful": 2,
        "times_noise": 0,
        "utility_ratio": 0.1,
    }
    assert _utility_weight(node, CFG) == 0.6  # 0.5 + 0.1


def test_missing_feedback_keys_treated_as_zero_exempt():
    # A well-retrieved node dict with NO feedback keys at all is also unmeasured
    # -> exemption fires (the `.get(..., 0) or 0` guards default to 0). This is the
    # common shape for the 36 affected nodes (utility_ratio computed, signals 0).
    node = {"retrieval_count": 10, "utility_ratio": 0.0}
    assert _utility_weight(node, CFG) == 1.0


def test_absent_center_degrades_to_uncentered_not_to_the_old_base():
    #  safety property: a cfg with NO utility_weight_center must default
    # the center to 0.0 -> w = 1.0 + ur (an uncentered weight), NOT silently fall
    # back to the old inverted 0.5 base. A missing key degrades to "no centering",
    # never to the bug the centering fixed.
    cfg = {k: v for k, v in CFG.items() if k != "utility_weight_center"}
    node = {
        "retrieval_count": 10,
        "times_helpful": 4,
        "times_inferred_helpful": 0,
        "times_noise": 0,
        "utility_ratio": 0.4,
    }
    assert _utility_weight(node, cfg) == 1.4  # 1.0 + 0.4, not 0.9


def test_center_equal_to_ratio_is_exactly_neutral():
    # The point of centering: a node whose utility_ratio sits AT the corpus
    # center is neither rewarded nor penalized relative to a never-retrieved
    # node (which returns the neutral 1.0). Pre-centering this same node scored
    # 0.5 + 0.2061 = 0.7061 — a ~29% penalty purely for having been measured.
    cfg = dict(CFG, utility_weight_center=0.2061)
    node = {
        "retrieval_count": 10,
        "times_helpful": 2,
        "times_inferred_helpful": 0,
        "times_noise": 0,
        "utility_ratio": 0.2061,
    }
    assert _utility_weight(node, cfg) == 1.0


def test_ordering_measured_good_beats_unmeasured_beats_measured_bad():
    # The ORDERING property is the whole point of centering, and it is the one
    # thing the isolated cases above do not assert. Pre-centering this ordering
    # was INVERTED: an unmeasured node scored 1.0 while a node with an
    # above-average utility_ratio of 0.30 scored 0.80, so no-track-record beat
    # proven. Pin the three-way relation, not just the individual values.
    cfg = dict(CFG, utility_weight_center=0.2061)
    sig = {"times_helpful": 3, "times_inferred_helpful": 0, "times_noise": 1}
    good = _utility_weight({"retrieval_count": 10, "utility_ratio": 0.30, **sig}, cfg)
    bad = _utility_weight({"retrieval_count": 10, "utility_ratio": 0.05, **sig}, cfg)
    unmeasured = _utility_weight({"retrieval_count": 2}, cfg)  # below neutral threshold
    assert good > unmeasured > bad, (good, unmeasured, bad)


if __name__ == "__main__":
    test_underretrieved_returns_neutral()
    test_zero_feedback_well_retrieved_is_exempt()
    test_noise_only_keeps_penalty()
    test_helpful_signal_uses_ratio_not_exemption()
    test_inferred_helpful_only_uses_ratio_not_exemption()
    test_missing_feedback_keys_treated_as_zero_exempt()
    test_absent_center_degrades_to_uncentered_not_to_the_old_base()
    test_center_equal_to_ratio_is_exactly_neutral()
    test_ordering_measured_good_beats_unmeasured_beats_measured_bad()
    print("all tests passed")
