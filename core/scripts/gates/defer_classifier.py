"""Defer-reason classifier — daemon-safe extraction (PR 7e/1).

Decides whether a `defer_reason` write is NARRATIVE (free-text claim about
an external signal that needs gate enforcement) or STRUCTURED (machine-
written internal state marker that must bypass the capability gate to
avoid keyword-colliding with forged skills).

Single source of truth for the STRUCTURED_DEFER_PREFIXES list. Used by:
  - aspirations.py cmd_update_goal (CLI path)
  - mind_api/src/endpoints/aspirations_write.py (daemon update_goal)
  - aspirations-precheck Phase 0.5b.4 (defer recheck; mirrors same predicate)

If you change the prefix list, search for STRUCTURED_DEFER_PREFIXES across
the repo — at least three call sites have to stay aligned.

Public API:
    is_narrative_defer(field, value) -> bool
    STRUCTURED_DEFER_PREFIXES   (tuple of canonical-case prefixes)

Daemon safety:
  - Pure function: no I/O, no env reads, no globals.
  - Lowercase comparison is precomputed at module load.
"""
from __future__ import annotations

from typing import Any


# Canonical-case prefixes (display order). The lowercased tuple below is what
# we match against; this one exists so existing error messages and docs can
# show the case the framework actually writes.
STRUCTURED_DEFER_PREFIXES = (
    "precondition_unmet:",        # aspirations-execute pre-claim re-check
    "blocked_on_dependency",      # dependency-chain tracker
    "Circuit breaker:",           # aspirations/SKILL.md Phase 5.5
    "human_blocked:",             # 6: genuinely non-agent-provisionable
                                  # human-gated block (user approve-click, legal
                                  # counsel, self-IAM). NEVER auto-clears, so
                                  # goal-selector exempts it from the 120h defer
                                  # fall-through (collect_eligible/collect_blocked)
                                  # — it stays suppressed-from-selector + counted-in-
                                  # blocked[] so quiescence can fire in gated
                                  # plateaus, instead of re-surfacing every iter.
                                  # User-surfacing preserved via the precheck
                                  # human_blocked age-escalation. Design: zeta's
                                  # 4 brief.
)

# CRITICAL: case-insensitive match. LLM-authored defers drift casing
# ("Circuit Breaker:", "circuit breaker:") across rewrites; a strict
# case-sensitive match would silently route drift through the capability
# gate and keyword-collide with forged skills (rb-246 pattern). Do NOT
# change to case-sensitive matching without first auditing every existing
# defer_reason in world/aspirations.jsonl and agent queues.
_STRUCTURED_DEFER_PREFIXES_LOWER = tuple(p.lower() for p in STRUCTURED_DEFER_PREFIXES)


def is_narrative_defer(field: str, value: Any) -> bool:
    """True iff this update writes a NARRATIVE defer_reason.

    Returns False for:
      - non-defer_reason fields (the gates only fire on defer_reason)
      - clears (None or empty string) — unblock paths must not need overrides
      - structured internal markers (precondition_unmet:, blocked_on_dependency,
        Circuit breaker:) — machine-written state that would otherwise
        keyword-collide with capability-gate scans

    Used by both the capability-gate defer-time check AND the blocker-ref
    requirement check — they share the trigger condition and MUST stay
    in sync. New gates with the same predicate should call this helper
    rather than re-inlining the three-way check.

    See `.claude/rules/probe-before-defer.md` and goal-schemas.md §
    Blocker Reference Schema for the doctrinal context.
    """
    if field != "defer_reason":
        return False
    if value is None or value == "":
        return False
    return not str(value).lower().startswith(_STRUCTURED_DEFER_PREFIXES_LOWER)
