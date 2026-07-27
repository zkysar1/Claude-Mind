"""test_capability_gate_event_gated_g1152943.py — event-gated / awaiting-
external-observation exemption (g-115-2943).

Background (the FP being fixed):
  The capability-gate defer->Unblock auto-conversion (probe-before-defer Layer
  D) MISFIRED on g-335-94's defer. Its defer_reason names an EXTERNAL EVENT
  blocker ("awaiting the FIRST organic post-swap DEV/ARC delete_environment to
  OBSERVE (fires when a live session ends — an external event)") — genuinely
  NON-provisionable (the agent cannot force an organic delete). But the 3-char
  token "arc" (from "DEV/ARC") coincidentally matched the forged skill
  measure-arc-two-arm-prereg, so keyword_block fired and a spurious HIGH
  "Unblock: create for g-335-94" (g-335-220, skipped) was auto-filed. None of
  the three existing non-provisionable detectors caught it: narrative patterns
  need "user"-phrasing, user-only preconditions need Roblox substrings, and the
  session-requirement regex needs "requires/needs/blocked-on X session" — the
  defer used "awaiting", which matches none.

Fix (structural, not a domain "arc" stopword band-aid):
  EVENT_GATED_PATTERNS + _match_event_gated_patterns detect the passive-wait-
  for-a-natural-event framing and SUPPRESS would_block uniformly (all three
  block types), exactly like user_keystroke_required does. The "arc" keyword
  STILL matches — we do not remove it — but an event-gated blocker is
  non-provisionable regardless of coincidental token collisions.

CRITICAL invariant (capability-before-user / guard-958): the exemption must
NOT over-suppress a genuinely PROVISIONABLE defer — that would let a real
capability-routing violation slip past (the g-115-792 anti-pattern). Every
pattern names PASSIVE waiting for a natural occurrence, so provisionable defers
("external service returned 500", "awaiting deploy", "cannot access efs") do
NOT match. The control test test_evaluate_provisionable_match_still_blocks is
the load-bearing no-over-suppression assertion.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

from gates.capability import (  # noqa: E402
    evaluate,
    EVENT_GATED_PATTERNS,
    _match_event_gated_patterns,
)

# 's ACTUAL failure_reason (the input that produced the FP ).
G33594_DEFER = (
    "Event-gated: awaiting the FIRST organic post-swap (>2026-07-17T15:17Z) "
    "DEV/ARC delete_environment to OBSERVE (fires when a live session ends "
    "— an external event"
)

# A minimal hermetic forged-skills.yaml: one skill whose trigger is a
# distinctive token, so a failure_reason naming it produces a GUARANTEED
# keyword match (no dependence on the live per-box registry).
# The distinctive token lives in the skill NAME (not just a trigger): a SOLE
# shared token qualifies as a match only when it is a name identifier part
# ( _single_token_qualifies) — exactly how "arc" in
# measure-arc-two-arm-prereg matched in the real incident.
_WIDGET_FORGED = (
    "skills:\n"
    "  widgetflux-tool:\n"
    "    triggers:\n"
    "      - widgetflux\n"
    "    companion_scripts: []\n"
)


def _make_world(tmp_path: Path, forged_yaml: str) -> Path:
    world = tmp_path / "world"
    world.mkdir(parents=True, exist_ok=True)
    (world / "forged-skills.yaml").write_text(forged_yaml, encoding="utf-8")
    return world


# ── _match_event_gated_patterns positive controls ────────────────────────────

def test_g33594_actual_text_matches_event_gated():
    m = _match_event_gated_patterns(G33594_DEFER)
    assert m, "g-335-94 defer_reason must be detected as event-gated"
    # multiple idioms present — any one suffices, but confirm the canonical set
    assert "awaiting the first organic" in m
    assert "an external event" in m


def test_each_pattern_matches_a_minimal_string():
    for p in EVENT_GATED_PATTERNS:
        assert _match_event_gated_patterns(f"precondition_unmet: {p} here") == \
            [q for q in EVENT_GATED_PATTERNS if q in f"precondition_unmet: {p} here"]
        assert p in _match_event_gated_patterns(f"blocked: {p}")


def test_event_gated_case_insensitive():
    assert _match_event_gated_patterns("AWAITING THE FIRST ORGANIC delete") == \
        ["awaiting the first organic"]
    assert "event-gated" in _match_event_gated_patterns("Event-Gated: waiting")
    assert "an external event" in _match_event_gated_patterns("It is An External Event.")


# ── _match_event_gated_patterns adversarial negatives (must NOT match) ────────
# Each is a PROVISIONABLE defer — the agent CAN act. If any of these matched,
# the exemption would wrongly suppress the block and the agent would defer real
# work (the capability-before-user /  anti-pattern).
_PROVISIONABLE_DEFERS = [
    "external service returned 500, retry the deploy",
    "awaiting deploy to complete",
    "waiting for CI run to finish",
    "the event handler needs wiring",
    "blocked, need to run provision_aws.py against staging",
    "requires a RUN-mode session to verify",
    "external dependency not yet installed",   # 'external' alone must not match
    "the event bus is misconfigured",          # 'event' alone must not match
]


def test_provisionable_defers_not_event_gated():
    for txt in _PROVISIONABLE_DEFERS:
        assert _match_event_gated_patterns(txt) == [], (
            f"OVER-SUPPRESSION regression: provisionable defer {txt!r} wrongly "
            f"classified event-gated"
        )


def test_empty_and_none():
    assert _match_event_gated_patterns("") == []
    assert _match_event_gated_patterns(None) == []


# ── evaluate(): suppression of a GUARANTEED capability match (hermetic) ───────

def test_evaluate_suppresses_matched_capability_when_event_gated(tmp_path):
    world = _make_world(tmp_path, _WIDGET_FORGED)
    fr = ("awaiting the first organic widgetflux occurrence to OBSERVE "
          "— an external event")
    r = evaluate(fr, intended_participants="user", suggest_unblock=True,
                 for_goal_id="g-test", world_dir=world)
    assert r["match_count"] >= 1, "widgetflux must match (guaranteed hermetic hit)"
    assert r["event_gated_detected"] is True
    assert r["would_block"] is False, (
        "event-gated blocker must SUPPRESS the block despite the capability match"
    )
    assert r.get("unblock_suggested", False) is False, (
        "no spurious Unblock may be suggested for an event-gated defer"
    )


def test_evaluate_provisionable_match_still_blocks(tmp_path):
    # SAME guaranteed widgetflux match, but NOT event-gated -> must still block.
    # This is the load-bearing no-over-suppression control: it proves the
    # suppression above is due to event-gating, not a broken match path.
    world = _make_world(tmp_path, _WIDGET_FORGED)
    fr = "blocked on user: invoke widgetflux now to proceed"
    r = evaluate(fr, intended_participants="user", suggest_unblock=True,
                 for_goal_id="g-test", world_dir=world)
    assert r["match_count"] >= 1, "widgetflux must match"
    assert r["event_gated_detected"] is False
    assert r["would_block"] is True, (
        "a provisionable (non-event-gated) capability match must still block"
    )


def test_evaluate_g33594_shape_suppressed(tmp_path):
    # Reproduce the incident SHAPE hermetically: a forged skill whose name
    # carries 'arc', plus 's actual defer_reason. Whether or not 'arc'
    # matches in this hermetic world, the event-gated framing must yield
    # would_block=False + event_gated_detected=True (the FP outcome fixed).
    arc_forged = (
        "skills:\n"
        "  measure-arc-two-arm-prereg:\n"
        "    triggers:\n"
        "      - arc\n"
        "    companion_scripts: []\n"
    )
    world = _make_world(tmp_path, arc_forged)
    r = evaluate(G33594_DEFER, intended_participants="user", suggest_unblock=True,
                 for_goal_id="g-335-94", world_dir=world)
    assert r["event_gated_detected"] is True
    assert r["would_block"] is False
    assert r.get("unblock_suggested", False) is False
