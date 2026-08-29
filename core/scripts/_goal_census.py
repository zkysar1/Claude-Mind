# SSOT for aspiration completion counts under goal eviction (B9-deep).
#
# WHY THIS EXISTS
# ---------------
# The goal queue grows without bound: every completed/abandoned goal stays in
# the aspiration's live `goals` list forever.  alone carried ~1300 aged
# terminal goals. On own-cloud (S3), the whole aspirations.jsonl is read+written
# per goal mutation, so an unbounded file is an unbounded per-op cost — the exact
# "crash the next phase on multiple machines" risk.
#
# The fix is EVICTION: aspirations-evict-completed.py removes aged terminal
# non-recurring goals from the live `goals` list (full records remain recoverable
# from the .history snapshot that locked_modify_jsonl takes before the write).
# But the scorer and every completion metric derive their ratios from the LIVE
# list (done_goals / total_goals). Naively dropping done goals would corrupt
# every ratio ( would read ~5% complete instead of ~93%), mis-rank the
# whole queue, and silently break consolidation pressure.
#
# THE INVARIANT
# -------------
# Eviction must be metric-neutral: every derived completion metric must be
# byte-identical before and after eviction. We cache the PER-STATUS counts of
# evicted goals on the aspiration as `archived_census: {by_status: {<status>: n}}`
# and have EVERY completion consumer compute its counts through effective_counts()
# below, which folds the census back in honoring that consumer's exact
# status-exclusion filter. A per-status (not {completed, abandoned}) census is
# required because the four live consumers use four DIFFERENT denominators — e.g.
# precheck-eval's consolidation ratio counts `superseded` as tracked while the
# scorer's "active" set excludes it. Only per-status counts reconstruct all four.
#
# Pre-eviction (no census) effective_counts is a pure pass-through over the live
# goals, so behavior is unchanged for any aspiration never evicted.
#
# This module is deliberately a LEAF (no imports from aspirations/goal-selector)
# so the precheck/scorer hot paths pay zero extra import cost. The literal
# ABANDONED_STATUSES below is guarded against drift from its definition sites
# (aspirations.TERMINAL_GOAL_STATUSES, strategic-pulse, precheck-eval) by
# tests/test_goal_eviction_invariance.py::test_abandoned_status_set_no_drift.

# Mirror of aspirations.TERMINAL_GOAL_STATUSES - {"completed"}. Kept as a literal
# (not imported) to preserve leaf status; drift-tested.
ABANDONED_STATUSES = frozenset({"skipped", "expired", "decomposed", "superseded"})
TERMINAL_STATUSES = ABANDONED_STATUSES | frozenset({"completed"})

# Goal-shape archived_census key. Absent census == no evicted goals.
CENSUS_KEY = "archived_census"


def census_evicted_ids(asp):
    """Return {status: [sorted goal-ids]} recorded by post-cutover eviction.

    g-115-2430: `evicted_ids` is the merge-correct census — an ID SET, so the
    cross-box reconcile (coordination_merge._merge_archived_census) unions it
    (commutative + idempotent) instead of LWW'ing a bare count, and the ids
    double as tombstones that stop _merge_goals resurrecting evicted goals.
    Tolerant of absent/partial/garbage shape; values normalized to sorted
    deduped string lists."""
    c = asp.get(CENSUS_KEY)
    if not isinstance(c, dict):
        return {}
    ids = c.get("evicted_ids")
    if not isinstance(ids, dict):
        return {}
    out = {}
    for status, v in ids.items():
        if not isinstance(v, list):
            continue
        vals = sorted({str(x) for x in v})
        if vals:
            out[status] = vals
    return out


def all_evicted_ids(asp):
    """Flat sorted list of every evicted goal id (all statuses). Consumed by the
    goal-id mint sites so max+1 allocation never re-mints an evicted id (which
    the merge-layer tombstone would then wrongly drop as a resurrection)."""
    out = set()
    for vals in census_evicted_ids(asp).values():
        out.update(vals)
    return sorted(out)


def census_by_status(asp):
    """Return {status: count} of evicted goals for an aspiration (possibly empty).

    Effective count per status = legacy `by_status` baseline (pre-g-115-2430
    count-only census, FROZEN at cutover: only census repairs shrink it) +
    len(evicted_ids[status]) (post-cutover id-set census). Legacy-only and
    ids-only aspirations both read correctly; pre-eviction aspirations return {}.

    Tolerant of absent/partial/garbage census so a hand-edited or pre-eviction
    aspiration never raises in the scorer."""
    c = asp.get(CENSUS_KEY)
    if not isinstance(c, dict):
        return {}
    out = {}
    bs = c.get("by_status")
    if isinstance(bs, dict):
        for status, n in bs.items():
            try:
                out[status] = max(0, int(n))
            except (TypeError, ValueError):
                continue
    for status, ids in census_evicted_ids(asp).items():
        out[status] = out.get(status, 0) + len(ids)
    return out


def census_completed(asp):
    """Count of evicted COMPLETED goals (0 when never evicted). Used by the
    cadence checks, which sum completed goals across the live queue + archive."""
    return census_by_status(asp).get("completed", 0)


def effective_counts(asp, *, exclude_statuses=frozenset(), include_recurring=True):
    """Return (total, completed) over the aspiration's goals, census-augmented.

    `exclude_statuses` / `include_recurring` reproduce each call site's exact
    denominator. Each existing site maps to one combination:

      exclude_statuses=ABANDONED_STATUSES, include_recurring=True
        -> goal-selector "active_goals" (completion_pressure, tail_bonus, the
           cross-asp ratio map, reward_history).
      exclude_statuses=frozenset(),        include_recurring=False
        -> recompute_progress / zombie scan "non_recurring" (abandoned included).
      exclude_statuses=ABANDONED_STATUSES, include_recurring=False
        -> strategic-pulse "_completion_ratio" (non-recurring AND non-abandoned).
      exclude_statuses={"skipped","expired","decomposed"}, include_recurring=True
        -> precheck-eval consolidation "tracked" (superseded counted as tracked).

    Eviction only ever removes NON-RECURRING TERMINAL goals, so:
      - an archived status is added back iff it is NOT excluded by this call's
        exclude_statuses (the recurring filter never drops archived goals because
        they are all non-recurring);
      - the completed tally grows only by archived `completed`.
    """
    goals = asp.get("goals") or []

    def _live(g):
        if not isinstance(g, dict):
            # A bare string ref or other non-record in the raw JSONL (a shape
            # the daemon now refuses at write time) is not a live goal; counting
            # it would raise here and fail every caller's census.
            return False
        if not include_recurring and g.get("recurring"):
            return False
        if g.get("status") in exclude_statuses:
            return False
        return True

    live = [g for g in goals if _live(g)]
    total = len(live)
    completed = sum(1 for g in live if g.get("status") == "completed")

    for status, n in census_by_status(asp).items():
        if status in exclude_statuses:
            continue
        total += n
        if status == "completed":
            completed += n
    return total, completed


def completion_ratio(asp, *, exclude_statuses=frozenset(), include_recurring=True):
    """Convenience: census-augmented done/total, 0.0 when total == 0."""
    total, completed = effective_counts(
        asp, exclude_statuses=exclude_statuses, include_recurring=include_recurring
    )
    return completed / total if total > 0 else 0.0
