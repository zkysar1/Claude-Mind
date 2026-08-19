"""Reflectability split for unreflected pipeline hypotheses ().

g-115-5358 widened `--unreflected` from live-only+stage==resolved to
live+archive+stage in (resolved, archived) — making the number TRUTHFUL
(the full never-reflected backlog). That was a semantics change for every
consumer using the value as a zero-test, threshold, or priority selector:
the backlog is dominated by records that can NEVER be reflected on
(g-115-4558: UNRESOLVABLE has no outcome to learn from; likewise EXPIRED
and outcome-less records). Measured 2026-08-14: 384 unreflected total =
181 UNRESOLVABLE + 150 EXPIRED + 47 no-outcome + 6 reflectable — and the
6 reflectable were exactly the 6 live stage=resolved records, i.e. in the
healthy flow "reflectable" and "awaiting reflection" coincide.

Consumers that gate ACTION (consolidation triage, quiescence drain
targeting, the iteration-close reflect nudge) must key on the REFLECTABLE
subset; the widened total remains the right number for backlog reporting.
This module is the one place that split is defined, so the next widening
changes every consumer together instead of one at a time.
"""

REFLECTABLE_OUTCOMES = {"CONFIRMED", "CORRECTED"}


def is_reflectable(rec) -> bool:
    """True when a pipeline record's outcome is one /reflect-on-outcome can
    actually learn from."""
    if not isinstance(rec, dict):
        return False
    return str(rec.get("outcome") or "").upper() in REFLECTABLE_OUTCOMES


def count_reflectable(records) -> int:
    """Count reflectable records in an --unreflected result array."""
    if not isinstance(records, list):
        return 0
    return sum(1 for r in records if is_reflectable(r))
