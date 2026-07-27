"""test_goal_source_cycle_detector_prefixes.py — regression for .

Pins the four automated detection-driven origin_signal prefixes
(alert-email:, routing-mismatch:, routing-either-resolve:, insight_trigger:)
in BOTH tables that must stay locked:
  - gates/origin_signal.py ALLOWED_PREFIXES (the gate whitelist)
  - _goal_source.py infer() (the goal_source SSOT mapping -> cycle-detector)

Root cause (g-115-986 audit / g-115-1100 fix): these prefixes were written in
production by auto-filers (inbox alert sweep, post-decompose routing audit,
insight-trigger sweep) but absent from both tables, so infer() returned None
and goal_source landed null on ~42% of recent asp-115 goals. The original
audit named three prefixes; the routing audit also emits the sibling
routing-either-resolve: (post-decompose-routing-audit.py:306), so this set
pins four.
"""
from __future__ import annotations

from _goal_source import infer
from gates.origin_signal import ALLOWED_PREFIXES, is_valid

# The four prefixes plus a representative concrete suffix for each.
SAMPLE_SIGNALS = {
    "alert-email:": "alert-email:s3-key-abc123",
    "routing-mismatch:": "routing-mismatch:g-115-1358",
    "routing-either-resolve:": "routing-either-resolve:g-001-17",
    "insight_trigger:": "insight_trigger:msg-20260611-120000-zeta-1900",
}
CYCLE_DETECTOR_PREFIXES = tuple(SAMPLE_SIGNALS)


def test_prefixes_in_allowed_prefixes():
    """Gate whitelist (table a) recognizes all four prefixes."""
    for pfx in CYCLE_DETECTOR_PREFIXES:
        assert pfx in ALLOWED_PREFIXES, f"{pfx} missing from ALLOWED_PREFIXES"


def test_gate_is_valid_accepts_sample_signals():
    """is_valid() accepts a concrete <prefix><suffix> for each."""
    for sig in SAMPLE_SIGNALS.values():
        assert is_valid(sig), f"is_valid rejected {sig}"


def test_infer_maps_to_cycle_detector():
    """SSOT mapping (table b) routes all four to cycle-detector."""
    for sig in SAMPLE_SIGNALS.values():
        assert infer(sig) == "cycle-detector", (
            f"infer({sig}) != cycle-detector (got {infer(sig)!r})")


def test_tables_locked():
    """Every prefix in this regression set is present in BOTH tables — the
    lock the g-115-1100 fix establishes (drift between the two write paths is
    the bug class this test guards)."""
    for pfx, sig in SAMPLE_SIGNALS.items():
        in_gate = pfx in ALLOWED_PREFIXES
        in_infer = infer(sig) == "cycle-detector"
        assert in_gate and in_infer, (
            f"{pfx} out of sync: gate={in_gate} infer={in_infer}")
