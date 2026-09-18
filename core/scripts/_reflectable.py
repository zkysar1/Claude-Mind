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


# ── Fixture tell () ────────────────────────────────────────────────
# The reflection queue is a WORK QUEUE whose prescribed action is a full ABC
# chain. It could not tell a TEST FIXTURE from a finding: both are resolved
# records carrying an outcome and a surprise score. Following the protocol
# literally over a fixture MANUFACTURES learning — fabricated ABC chains, belief
# updates and pattern signatures from a claim that was never a prediction, and
# the artifacts are indistinguishable from real ones afterward.
#
# It FLAGS, it never filters (guard-1072: mark residue in place; never remove
# from a union-by-id merged store). A silent exclusion would hide the residue
# from the only queue positioned to notice it.
#
# WHY THE KEY IS ALWAYS EMITTED, even when empty: a key that appears only when
# non-empty cannot distinguish "nothing suspect" from "this build has no tell",
# and a consumer written against the second reading silently loses the guard.
#
# TWO SIGNALS, and the measured reason there are two (alpha, cc-07, 2026-08-28,
# over the live+archive union, 1809 records):
#   fixture-slug-category  — catches 8, ALL 8 genuinely fixtures, 0 false
#                            positives, and 5/5 of the named  control.
#   duplicate-title        — catches exactly the 5  records. One claim
#                            resolving three ways (CONFIRMED/CORRECTED/CONFIRMED
#                            on an identical title) is a test matrix; no real
#                            prediction can produce it (foxtrot, 2026-08-03).
#
# THE SECOND SIGNAL ADDS CORROBORATION, NOT COVERAGE — measured, and written
# down here so a future maintainer can subtract it on evidence instead of
# re-deriving it. Those 5 are a strict SUBSET of the category signal's 8: over
# 1809 records, duplicate-title catches ZERO that fixture-slug-category misses.
# It is kept because the category signal is a slug ALLOWLIST and therefore
# depends on the fixture author's naming, while duplicate-title is a shape
# signal that does not — and because a flag carrying TWO independent reasons is
# readable as strong where one reason is readable as a category-name
# coincidence. If a later corpus still shows 0 unique catches AND the
# corroboration is not being used, delete it and drop the two-pass tally with
# it; that is the whole cost.
#
# Queue-scoped, which is the population the endpoint actually stamps: 4 of 442
# unreflected flagged (all fixture-slug-category; the other 4 corpus-wide flags
# are already reflected:true and so never enter the queue), key present on
# 442/442.
#
# A PREDICATE THAT LOOKED PERFECT AND WAS NOT — recorded so it is not re-derived.
# A "skeletal" conjunct (no rationale AND no evidence AND no outcome_detail)
# scored 5/5 with zero false positives when tuned on the UNREFLECTED QUEUE, and
# scored 1/5 on the full corpus: 4 of the 5  fixtures DO carry
# outcome_detail (it names the derivation test), and only census-d — the one
# still in the queue — is skeletal. The queue is a biased sample of the corpus
# because the other four are already reflected:true. Tune a detector on the
# population its positive control lives in.
#
# KNOWN MISS, accepted deliberately: 2026-04-20_test-valid (title "Test valid",
# no rationale/evidence/outcome_detail) is a fixture by inspection but carries
# category `framework-test`, which is ALSO a legitimate category. Widening the
# slug set to catch it would risk flagging real framework-test hypotheses, and
# guard-1665's negative control matters more than the marginal catch. Under-flag
# rather than false-positive.

FIXTURE_SLUG_CATEGORIES = {
    "test", "test-cat", "test-category", "foo", "bar", "baz", "tmp", "dummy",
}


def _title_key(rec) -> str:
    return str((rec or {}).get("title") or "").strip()


def fixture_suspect_reasons(rec, duplicate_titles=frozenset()) -> list:
    """Reasons this record looks like a test fixture. Empty list == clean.

    `duplicate_titles` is the set of title strings appearing on 2+ records in
    the corpus being annotated; pass it from annotate_fixture_suspects, which
    computes it. Per-record purity is deliberate so the signal is unit-testable
    without a store.
    """
    if not isinstance(rec, dict):
        return []
    reasons = []
    if str(rec.get("category") or "").strip().lower() in FIXTURE_SLUG_CATEGORIES:
        reasons.append("fixture-slug-category")
    t = _title_key(rec)
    if t and t in duplicate_titles:
        reasons.append("duplicate-title")
    return reasons


def annotate_fixture_suspects(records):
    """Stamp `fixture_suspect` (a list of reasons) on every record IN PLACE.

    Always sets the key — empty list when clean — so absence means "old build",
    not "clean". Returns the same list for call-site convenience.
    """
    if not isinstance(records, list):
        return records
    counts = {}
    for r in records:
        if isinstance(r, dict):
            t = _title_key(r)
            if t:
                counts[t] = counts.get(t, 0) + 1
    dupes = frozenset(t for t, n in counts.items() if n > 1)
    for r in records:
        if isinstance(r, dict):
            r["fixture_suspect"] = fixture_suspect_reasons(r, dupes)
    return records


# ── Ownership split () ────────────────────────────────────────────
# WHY THIS LIVES BESIDE is_reflectable AND NOT IN EITHER CALLER. Three consumers
# gate on "is there reflection work for ME": review-hypotheses Mode 2 (which
# reflects), the iteration-close nudge (which asks), and 's string
# precondition (which selects). They disagreed with guard-5623, which says the
# owner is `resolved_by` and a live owner's records must be left alone: the
# instrument counted every reflectable record, so the nudge fired on records the
# agent had to abstain from, on every close, forever. That is the guard-1984
# shape -- a guardrail cannot outvote the instrument it guards -- so the split
# belongs in the instrument, beside the split it already owns.
#
# THE FILTER POINTS BOTH WAYS, AND THE SECOND HALF IS THE ONE THAT PRODUCES
# WORK. Measured (echo, cc-03, 2026-09-15): an un-split count of 13 was read as
# "all bravo's -> bravo is alive -> abstain" across at least two iterations
# while echo itself owned THREE CORRECTED records in that same queue. The
# cheapest reading of an un-split number is "someone else's", and nothing ever
# contradicts it. So callers get `mine` BY ID, never just a total to subtract
# from. A filter that only subtracts would have left the real debt untouched.
#
# SCOPE IS AN EXPLICIT REQUIRED PARAMETER (guard-2601). `self_agent` and
# `liveness` are passed in; this module resolves neither. A predicate that read
# MIND_AGENT itself would answer a different question than the caller asked the
# moment any caller ran on behalf of another agent, and a silent always-"held"
# is indistinguishable from a legitimate abstention. Required, never
# optional-with-default -- a default is what lets a future caller re-open the
# hole while still compiling.
#
# LIVENESS IS INJECTED, NOT PROBED. `liveness` maps agent-name -> verdict string
# as `liveness-check.sh --agent X --json` reports it. Keeping the subprocess out
# is what makes the four cases below unit-testable without a live fleet, and it
# lets a caller probe N distinct owners once each instead of once per record
# (the queue is dominated by a single owner, so that is ~1 probe, not ~13).
#
# UNKNOWN LIVENESS ABSTAINS. guard-5623 permits proceeding on
# "dormant/retired/unknown-with-corroboration"; this module cannot see
# corroboration, so an absent or unrecognised verdict classifies `held`. The
# conservative direction is the one where the cost is a delayed reflection
# rather than two conflicting ABC chains on one record, where the loser is
# silent by construction.
#
# SELF IS DECIDED BEFORE ANY LIVENESS LOOKUP (guard-6259: never judge your own
# liveness). `mine` short-circuits, so a self-probe is never even constructed.
#
# THE STRANDED-OWNER RULE, AND WHY IT IS AN AGE RULE RATHER THAN A DEADLOCK
# BREAKER. The original framing argued a pure ownership filter hides a live
# owner's records from everyone else FOREVER and therefore needs a deadlock
# breaker. That absolute is FALSIFIED (alpha, cc-04, 2026-09-15): over the
# resolved stage, 19 records were reflected in one day and the dominant owner
# reflected TWO OF ITS OWN inside that window, so records are not frozen. What
# the same measurement does show is a RATE problem -- n=2/day against a 13-deep
# queue that owner created in 15h. So the breaker keys on AGE SINCE resolved_at,
# not on a deadlock that does not exist.
#
# 72h is derived, not picked: the owner's own learn cadence () runs on a
# 60h interval, so 72h is one full owner-cadence interval plus a 12h margin. A
# record still unreflected at 72h has survived at least one firing of the
# cadence that exists to drain it, which is the earliest point at which "the
# owner is not getting to this" is evidence rather than impatience. Reclaiming
# is announced (the caller posts), never silent -- guard-1072's mark-in-place
# discipline applied to work rather than to records.

OWNERSHIP_ACTIONABLE = frozenset({"mine", "unowned", "reclaimable"})
RECLAIMABLE_VERDICTS = frozenset({"dormant", "retired"})
STRANDED_HOURS = 72


def ownership_of(rec, self_agent, liveness, now=None,
                 stranded_hours=STRANDED_HOURS):
    """Classify one resolved record's reflection ownership.

    `self_agent` (str) and `liveness` (dict agent -> verdict) are REQUIRED --
    see guard-2601 in the block above. `now` is injectable for tests.

    Returns exactly one of:
      "mine"        -- resolved_by is self_agent. Reflect it; it is your debt.
      "unowned"     -- no resolved_by at all. Nobody can be racing a record
                       nobody resolved, so it is actionable by whoever finds it.
      "reclaimable" -- the owner is dormant or retired, OR is alive but the
                       record has sat past `stranded_hours` since resolved_at.
                       Reflect it AND announce the reclaim on the board.
      "held"        -- another agent owns it and is alive (or its liveness is
                       unknown/unrecognised). ABSTAIN -- guard-5623.
    """
    if not isinstance(rec, dict):
        return "held"
    owner = str(rec.get("resolved_by") or "").strip()
    if not owner:
        return "unowned"
    if owner == str(self_agent or "").strip():
        return "mine"
    verdict = str((liveness or {}).get(owner) or "").strip().lower()
    if verdict in RECLAIMABLE_VERDICTS:
        return "reclaimable"
    # Owner is alive, or its liveness is unknown. Only the age rule can free it.
    if verdict == "alive":
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from _dt import parse_naive_iso
        from datetime import datetime
        resolved_at = parse_naive_iso(rec.get("resolved_at"))
        if resolved_at is not None:
            ref = now or datetime.now()
            if (ref - resolved_at).total_seconds() >= stranded_hours * 3600:
                return "reclaimable"
    return "held"


def split_by_owner(records, self_agent, liveness, now=None,
                   stranded_hours=STRANDED_HOURS):
    """Bucket REFLECTABLE records by ownership. Non-reflectable ones are dropped.

    Returns a dict with per-bucket id lists, an `actionable` count (mine +
    unowned + reclaimable), and `held_by` -- the per-owner tally of what was
    abstained from, so a caller can report WHO it is waiting on rather than
    just how many. Callers must report the abstain count separately from the
    actionable one; an un-split total is the defect this module exists to fix.
    """
    out = {"mine": [], "unowned": [], "reclaimable": [], "held": [],
           "held_by": {}, "actionable": 0, "reflectable_total": 0}
    if not isinstance(records, list):
        return out
    for r in records:
        if not is_reflectable(r):
            continue
        out["reflectable_total"] += 1
        bucket = ownership_of(r, self_agent, liveness, now=now,
                              stranded_hours=stranded_hours)
        rid = (r or {}).get("id")
        out[bucket].append(rid)
        if bucket == "held":
            owner = str((r or {}).get("resolved_by") or "").strip() or "?"
            out["held_by"][owner] = out["held_by"].get(owner, 0) + 1
    out["actionable"] = sum(len(out[b]) for b in ("mine", "unowned",
                                                  "reclaimable"))
    return out


def owners_of(records):
    """Distinct non-empty resolved_by values among REFLECTABLE records.

    The caller probes liveness for exactly these, once each, and passes the
    result back in as `liveness` -- N distinct owners, not N records.
    """
    if not isinstance(records, list):
        return []
    seen = []
    for r in records:
        if not is_reflectable(r):
            continue
        owner = str((r or {}).get("resolved_by") or "").strip()
        if owner and owner not in seen:
            seen.append(owner)
    return seen
