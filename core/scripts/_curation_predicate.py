#!/usr/bin/env python3
# domain-leak-exempt: framework curation predicate — reasoning-bank/guardrail utilization fields are framework store schema, not domain data
"""Curation predicate — the active-forgetting seam (single source of truth).

Extracted from ``bulk-retire-dead-entries.py`` (g-336-56) so BOTH the production
retirement tool AND the MemEvoBench governance-eval
(``core/scripts/memevo_bench.py``) call the SAME pure predicate — they can never
drift. This is the "harness seam" the governance-evals spec (§6) names for the
Mind consolidation loop: a deterministic pure function over an entry's
utilization fields → keep/retire, with NO wall-clock read, NO I/O, and NO path
resolution (``today`` is caller-supplied so the function reads no clock).

``is_dead_entry`` is the memory-misevolution DEFENSE: an entry that accumulated
significant retrieval volume but produced ZERO helpful signal (no explicit
helpful event, no citation, no inferred-helpful backstop) over a minimum age is
a self-poisoning dead entry — it inflates ``retrieval_count`` and crowds
retrieval without adding value (the 2026-05-09 knowledge-system audit found ~75%
of the reasoning bank in exactly this state). Active forgetting retires it; an
unguarded update loop lets it accumulate — that accumulation IS memory
misevolution (self-poisoning by the update loop, arXiv 2604.15774).

The zero-check counts ``times_inferred_helpful`` (the automatic
retrieval-application backstop) alongside explicit helpful/cited, mirroring the
rest of the utilization system's ``utility_ratio = (th + 0.5*tih)/rc`` so a
heavily-retrieved entry attested only by inference is not false-positive-retired
(g-115-1605).
"""
from __future__ import annotations

from datetime import date, datetime


def parse_created(rec: dict):
    """Parse the ``created`` field (``YYYY-MM-DD`` date or ISO datetime).

    Returns a ``datetime.date`` or ``None`` when absent/unparseable.
    """
    val = rec.get("created", "") or ""
    if not val:
        return None
    try:
        if "T" in val:
            return datetime.strptime(val[:10], "%Y-%m-%d").date()
        return datetime.strptime(val, "%Y-%m-%d").date()
    except Exception:
        return None


def is_dead_entry(
    rec: dict,
    min_retrievals: int,
    min_age_days: int,
    today: date,
    counters=None,
) -> bool:
    """True iff ``rec`` is a heavily-retrieved, never-helpful, aged entry — the
    active-forgetting retirement criterion.

    Pure: ``today`` (a ``datetime.date``) is caller-supplied, so the function
    reads no wall-clock and is fully deterministic given its inputs.

    ``counters`` (g-358-05) is the caller-supplied sidecar map, id -> counters.
    It is a PARAMETER rather than a load precisely to keep the purity contract
    above: this module resolves no path and does no I/O, and it is the shared
    seam ``memevo_bench`` evaluates, so a load here would make the governance
    eval depend on the live filesystem. Default None => read the embedded field,
    which is byte-identical to pre-seam behaviour.

    THIS IS THE HIGHEST-CONSEQUENCE JOIN IN THE SPLIT. Once the writer lands,
    the embedded field is a frozen pre-split snapshot: an entry whose counters
    have since moved would be judged on stale zeros and RETIRED while live.
    Retirement is a write, so getting this one wrong loses knowledge rather than
    merely misreporting it.
    """
    if rec.get("status") != "active":
        return False
    # Already pending retirement (or in some other lifecycle state).
    if rec.get("retirement_date"):
        return False
    if counters:
        # Deferred import, and deliberately inside the `counters` branch. At
        # module scope this would resolve WORLD_DIR at import time and break the
        # no-path-resolution contract for every caller including memevo_bench;
        # here it runs only when a caller has already imported the seam to build
        # `counters`, so the module is in sys.modules and the import is free.
        # Reusing `utilization_of` rather than re-typing its sidecar-wins
        # precedence keeps one implementation of that rule (guard-2676).
        from _utilization_store import utilization_of as _uo
        util = _uo(rec, counters)
    else:
        util = rec.get("utilization") or {}
    rc = util.get("retrieval_count", 0) or 0
    helpful = util.get("times_helpful", 0) or 0
    cited = util.get("times_cited", 0) or 0
    inferred = util.get("times_inferred_helpful", 0) or 0
    if rc < min_retrievals:
        return False
    if (helpful + cited + inferred) > 0:
        return False
    created = parse_created(rec)
    if created is None:
        return False
    age = (today - created).days
    if age < min_age_days:
        return False
    return True
