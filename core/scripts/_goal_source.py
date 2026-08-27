"""Goal-source inference () — single source of truth.

PARITY: imported by BOTH
  - core/scripts/aspirations.py            (fallback / direct-python path)
  - mind_api/src/endpoints/aspirations_write.py  (daemon hot path)
  - core/scripts/backfill-goal-source.py   (one-shot backfill)
If a new entry point files goals, import apply_default here — do NOT
duplicate the inference logic. Drift between the two write paths leaves
some goals with `goal_source` populated and others null, breaking the
framework-vs-domain attribution US-06 depends on.

The prefix→source mapping below is mirrored in
core/config/conventions/goal-schemas.md "Auto-derivation from origin_signal".
That table is the canonical reader-facing reference; keep the two locked.
"""
from __future__ import annotations

VALID_GOAL_SOURCES = frozenset({
    "user", "agent-self", "recurring-cycle",
    "cycle-detector", "forge-skill",
})
# NOT a member, deliberately: "chat-originated". The chat-goal lane
# (core/config/chat-goal-protocol-digest.md) originally prescribed it as a
# goal_source, which is a category error this set is the SSOT for --
# goal_source answers WHO INITIATED (a chat request is initiated by the user),
# while "chat-originated" answers WHICH LANE FILED IT. Overloading the WHO
# vocabulary with a HOW value would have made every user-sourced consumer
# (drift denominators, US-06 attribution) blind to chat work. The lane
# discriminator lives in origin_signal as the sanctioned "chat-goal:" prefix
# below, which infers back to "user" -- so the goal is countable AS a lane and
# still classified correctly AS user-initiated. (, 2026-08-26.)


def infer(origin_signal):
    """Map an `origin_signal` value to a `goal_source` enum value, or None
    when the signal does not map cleanly. None contributions are excluded
    from drift denominators (not penalized).
    """
    if not origin_signal or not isinstance(origin_signal, str):
        return None
    sig = origin_signal.strip()
    if (sig == "user_directive"
            or sig.startswith("pending_question:")
            or sig.startswith("user-directed:")
            or sig.startswith("user_directed:")
            # chat-goal lane (): a substantive assistant-mode request
            # filed as a goal record. User-initiated, so it infers "user" like
            # its siblings; the prefix is what makes the LANE countable without
            # a second vocabulary. Keep locked with ALLOWED_PREFIXES.
            or sig.startswith("chat-goal:")):
        return "user"
    if sig.startswith("recurring_cadence:") or sig.startswith("recurring:"):
        return "recurring-cycle"
    if sig.startswith(("failing_test:", "resolved_hypothesis:",
                       "low_confidence_node:", "drift_detected:", "monitor:",
                       # automated detection-driven filers () — kept
                       # locked with ALLOWED_PREFIXES in gates/origin_signal.py
                       "alert-email:", "routing-mismatch:",
                       "routing-either-resolve:", "insight_trigger:",
                       # second reconciliation () — same lane, found
                       # by diffing prescribed SKILL.md literals against
                       # ALLOWED_PREFIXES; both are automated sweeps
                       "skill-discovery-audit:", "blocker_pattern:",
                       # third reconciliation () — unit-economics-readout.py
                       # threshold-move auto-file; without it the Layer-D rewrite
                       # made this automated filer infer as "agent-self", i.e.
                       # misattributed rather than null
                       "unit-economics-move:")):
        return "cycle-detector"
    if sig == "idle_fallback" or sig.startswith((
            "decomposition:", "parent_aspiration:", "unblock:",
            "investigate:", "investigation:",
            "idea:", "maintain:",
            "apply:", "brief:",
            "board_post:",
            # fresh-eyes program-review proposals (agent's own review cadence)
            "program-change-proposal:")):
        return "agent-self"
    return None


def apply_default(goal):
    """Mutate `goal` in place: set goal_source from origin_signal when absent.

    Explicit caller value always wins; this only fills None / missing keys.
    No-ops when origin_signal does not map.
    """
    if "goal_source" not in goal or goal.get("goal_source") is None:
        inferred = infer(goal.get("origin_signal"))
        if inferred is not None:
            goal["goal_source"] = inferred
