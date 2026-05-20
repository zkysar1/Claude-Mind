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
            or sig.startswith("user_directed:")):
        return "user"
    if sig.startswith("recurring_cadence:") or sig.startswith("recurring:"):
        return "recurring-cycle"
    if sig.startswith(("failing_test:", "resolved_hypothesis:",
                       "low_confidence_node:", "drift_detected:", "monitor:")):
        return "cycle-detector"
    if sig == "idle_fallback" or sig.startswith((
            "decomposition:", "parent_aspiration:", "unblock:",
            "investigate:", "investigation:",
            "idea:", "maintain:",
            "apply:", "brief:",
            "board_post:")):
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
