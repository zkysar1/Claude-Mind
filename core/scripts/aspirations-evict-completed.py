#!/usr/bin/env python3
"""Evict AGED, TERMINAL, non-recurring goals out of the live aspirations queue —
the deep, growth-bounding fix for B9 (asp-115 add-goal latency + hot-lock
contention on world/aspirations.jsonl under own-cloud).

WHY EVICT, NOT JUST COMPACT (the deeper redesign)
  aspirations-compact-completed.py strips the bulky TEXT of aged-completed goals
  but KEEPS the record in the live list, so the live file still grows without
  bound (just ~9x slower). Eviction REMOVES the aged terminal record from the
  live list entirely, so the live file is bounded by {non-terminal goals +
  recently-terminal goals} — terminal work ages out instead of accumulating
  forever. That is the actual fix for unbounded growth, not a deferral of it.

  The classic objection to eviction is that goal-selector.py and every other
  completion metric derive their ratios from LIVE goal counts (done/total), so
  dropping done goals would crater completion_ratio / completion_pressure /
  tail_bonus / the zombie scan / progress. B9-deep removes that objection: a
  PER-STATUS census (`archived_census.by_status`) is cached on the aspiration and
  every completion consumer reads its counts through _goal_census.effective_counts,
  which folds the census back in honoring that consumer's exact denominator. The
  result is metric-NEUTRAL eviction — every derived value is byte-identical
  before and after. See _goal_census.py and tests/test_goal_eviction_invariance.py.

WHAT IS EVICTED
  A goal is eligible iff ALL hold:
    - status in TERMINAL_STATUSES (completed | skipped | expired | decomposed |
      superseded)
    - NOT recurring (recurring goals never terminate; they stay live forever)
    - its terminal date (completed_at | completed_date | last_modified) is MORE
      than --age-days ago (default 45 — well beyond completion-report lookback
      (~7d), reflection lookback, and trajectory's velocity window (last 5), so
      no record-consumer loses signal it actually uses)
      ⚠ THAT DERIVATION IS ABOUT THE DEFAULT AND THE FLEET DOES NOT RUN THE
      DEFAULT. The shipped cadence is age_days: 3 (operator-approved "go 3",
      2026-08-14; core/config/aspirations.yaml § aspirations_eviction), which is
      INSIDE the ~7d completion-report lookback this sentence relies on — and the
      margin was never re-derived when the threshold moved 15x tighter. Measured
      2026-08-18 (alpha, cc-04): terminal non-recurring goals in the live store
      bucket 194/447/113/54 at ages 0/1/2/3d and then EXACTLY ZERO from day 4,
      while non-terminal goals (which eviction never touches) tail smoothly to
      99d. agent-completion-report step 2 filters goal RECORDS on `completed_at
      >= since`, so any report window wider than ~3d under-reports. The other two
      consumers named above (reflection lookback, trajectory velocity window) are
      UNCHECKED. Tracked by g-115-6659 — do not treat this parenthetical as a
      standing safety guarantee at the deployed value.
      SELF-RETIRING: this block restates a value it does not own (the SSOT is
      core/config/aspirations.yaml § aspirations_eviction), so it is subject to
      the very drift guard-4282 describes. Its invalidation condition is written
      into it: if age_days is ever raised above the ~7d lookback, the tension
      described here no longer exists — DELETE this block rather than updating
      the number, and re-derive the margin against whatever the new value is.
  Recurring goals, live work (pending/blocked/in-progress), and recently-terminal
  goals are NEVER evicted. Undateable goals are conservatively SKIPPED (never
  evicted on a guess).

WHERE THE RECORDS GO
  Out of the live list, and NOT re-homed anywhere queryable by id. We do NOT
  append individual goal records to aspirations-archive.jsonl: that file holds
  whole-ASPIRATION records (each line a nested {id, goals:[...]}), and the cadence
  checks count it by iterating each line's `goals`. Writing goal-shaped lines
  there would be invisible to that count and would require a non-atomic two-file
  write. What IS retained is the per-status census + evicted-id set, which is what
  every completion consumer actually reads (see _goal_census.py). Census is
  simpler and crash-atomic (one locked write).

  DO NOT read .history as a record-recovery path. locked_modify_jsonl does take a
  snapshot immediately before the write, but it is a whole-FILE blob keyed by
  write TIME with no goal-id index, under tiered retention (<=7d keep all, 8-30d
  latest-per-day, 31+ latest-per-week) — and `census_is_legacy_blind` below
  records evicted-id recovery from it as infeasible in practice. An earlier
  version of this paragraph claimed the opposite ("the full records remain
  recoverable from the .history snapshot"); measured 2026-08-11, that sentence
  sent an investigation hunting a store that cannot answer the question.

  CONSEQUENCE — A GOAL ID IS EPHEMERAL BY DESIGN. It stops resolving ~45 days
  after the goal goes terminal AT THIS SCRIPT'S DEFAULT; at the cadence the fleet
  actually runs (age_days: 3) the fuse is ~3 DAYS, which makes everything below
  15x more urgent rather than less — the 39% figure was measured against the 45d
  horizon and is a FLOOR, not a ceiling. Only recurring and non-terminal goals
  persist indefinitely. So do not cite goal ids in PERMANENT artifacts (code comments,
  rules, conventions) as if they were durable references — cite the append-only
  stores instead (rb-NNN / guard-NNN), which nothing evicts. Measured 2026-08-11:
  of 3432 distinct goal ids cited across core/ + .claude/ + CLAUDE.md, 1357 (39%)
  already resolve to nothing, and the remainder are on the same rolling fuse.
  (This paragraph cites dates rather than goal ids on purpose.)

SAFETY
  - Routes through _fileops.locked_modify_jsonl -> DDB lock (cross-machine mutex)
    + force-fresh-from-S3 read + If-Match fenced PUT + .history snapshot +
    post-write JSONL canary. Same path the daemon uses; under own-cloud the
    scoped MIND_AWS_* creds are used (fail-closed in OwnCloudBackend.from_env).
  - HARD INVARIANT baked into the modifier: for every aspiration, all four
    completion denominators AND the cadence completed-count are recomputed before
    and after the move; ANY mismatch raises and the locked write is aborted (no
    write lands). This is the runtime twin of the unit invariance test.
  - Idempotent: a re-run finds the already-evicted goals gone from the live list
    and the census already reflecting them, so it evicts only newly-aged goals.
  - DRY-RUN by default. --apply performs the write.

USAGE
  Dry-run (read-only; reports projected eviction):
    set -a; source .env.local; set +a   # STORAGE_BACKEND + scoped creds
    MIND_AGENT=alpha py -3 core/scripts/aspirations-evict-completed.py --source world
  Apply (own-cloud backend — the --apply WRITE needs the governed-root map too):
    source core/scripts/_paths.sh                                     # sets WORLD_PATH/META_PATH (shell-local only)
    export MIND_WORLD="$WORLD_PATH"; export MIND_META="$META_PATH"  # guard-879: _paths.sh does NOT export these to subprocesses; OwnCloudBackend.from_env needs one of MIND_WORLD/WORLD_PATH or MIND_META/META_PATH in the SUBPROCESS env or the --apply write aborts BEFORE the lock ("neither ... is set — cannot map a governed path to a root"). Dry-run does not hit this (read-only path resolution uses the Python _paths import). Local backend needs none of this.
    set -a; source .env.local; set +a                                # STORAGE_BACKEND + scoped MIND_AWS_* creds
    MIND_AGENT=alpha py -3 core/scripts/aspirations-evict-completed.py --source world --apply
    ... then repeat with --source agent --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR  # noqa: E402
from _goal_census import (  # noqa: E402
    ABANDONED_STATUSES, TERMINAL_STATUSES, CENSUS_KEY, effective_counts,
    census_completed, census_by_status, census_evicted_ids, all_evicted_ids,
)

# The exact denominators the live consumers use — recomputed before/after each
# eviction to prove metric-neutrality. (exclude_statuses, include_recurring, label)
_INVARIANT_VARIANTS = (
    (ABANDONED_STATUSES, True, "scorer_active"),                 # goal-selector
    (frozenset(), False, "non_recurring"),                       # recompute/zombie
    (ABANDONED_STATUSES, False, "pulse"),                        # strategic-pulse
    (frozenset({"skipped", "expired", "decomposed"}), True, "precheck_tracked"),
)


def _terminal_dt(goal: dict):
    """Best-effort terminal datetime, or None (None -> conservative skip)."""
    for field in ("completed_at", "completed_date", "last_modified"):
        raw = goal.get(field)
        if not raw:
            continue
        s = str(raw)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19] if "T" in s else s[:10], fmt)
            except ValueError:
                continue
    return None


def _eligible(goal: dict, cutoff: datetime) -> bool:
    """True iff this goal should be evicted (aged, terminal, non-recurring)."""
    if goal.get("recurring"):
        return False
    if goal.get("status") not in TERMINAL_STATUSES:
        return False
    dt = _terminal_dt(goal)
    if dt is None:
        return False  # cannot confirm age -> conservative skip
    return dt < cutoff


# Sequence-space goal ids: g-NNN-NN..NNNN with an optional concurrent-mint /
# decompose suffix letter (see coordination_merge._union_goals re-key grammar).
# Widths are fully open-ended per guard-1161: a bounded form (\d{3}, \d{2,4})
# silently drops both legacy low-end ids and any aspiration that outgrows the
# ceiling. This pattern held `\d{3}-\d{2,4}` until , which left
#  one mint (0) away from the goal ids in its own queue
# becoming invisible to the conservation guard.
_GOAL_SEQ_RE = re.compile(r"^g-(\d+)-(\d+)(-[a-z])?$")


def _asp_num(asp) -> str:
    """The numeric part of an aspiration id ( -> '335'), or None when the
    id is not of that shape. None means UNSCOPED matching in `_parse_seq_id`,
    which is `_conservation_violation`'s long-standing contract; all 19 live
    aspiration ids match `^asp-\\d+$`, so the unscoped branch is unreachable on
    real data and is pinned as such in test_capacity_suffix_parity.py."""
    aid = str(asp.get("id", "") if isinstance(asp, dict) else "")
    return aid[len("asp-"):] if aid.startswith("asp-") else None


def _parse_seq_id(gid, asp_num):
    """THE parser for the sequence-space goal-id grammar. Returns
    (seq:int, is_suffixed:bool) for an id belonging to this aspiration's
    sequence space, else None.

    SINGLE SOURCE OF TRUTH, and that is the entire point (g-115-3868). This file
    used to carry FOUR readings of one grammar: `_GOAL_SEQ_RE` here,
    a `rstrip("a-z")` heuristic in `_capacity`, and a naive `startswith(prefix)`
    in BOTH `_audit_violations` and `_repair`. g-115-4270 patched the rstrip half
    to also strip a trailing hyphen — which fixed the live symptom and left the
    divergence standing. Measured 2026-08-10 over 7,213 live ids: the two parsers
    now agree on 100% of them, and still disagree on `12a`, `12ab` and `12-ab`,
    where the heuristic counted and the regex skipped.

    The naive prefix match was the other half of the apples-to-oranges the
    original incident named: `in_list` counted the four `g-335-262-a..d` legs
    while capacity did not, so the two sides of the pigeonhole inequality were
    computed off different id sets. Both sides now come from here, so an id
    contributes to BOTH sides or to NEITHER — which is what makes a false
    violation unconstructable rather than merely absent today.

    Ids outside the grammar (`g-xw-<ts>-NN` cross-world ids, foreign prefixes,
    `g-NNN-12-a-b`) are deliberately outside BOTH sides."""
    m = _GOAL_SEQ_RE.match(str(gid or ""))
    if not m or (asp_num is not None and m.group(1) != asp_num):
        return None
    return int(m.group(2)), bool(m.group(3))


def _in_list_sequence_goals(asp) -> int:
    """Count of live goals inside this aspiration's sequence space — the left
    side of the pigeonhole inequality, read through `_parse_seq_id` so it can
    never disagree with `_capacity` about which ids exist."""
    asp_num = _asp_num(asp)
    return sum(1 for g in (asp.get("goals") or [])
               if _parse_seq_id((g or {}).get("id"), asp_num) is not None)


def _legacy_census_loose(asp: dict, in_list: int, capacity: int) -> bool:
    """: is the pigeonhole capacity a KNOWN-loose lower bound here?

    True iff the census is 100% LEGACY count-only (no recorded evicted_ids,
    pre-g-115-2430) AND the aspiration is not-all-live (in_list < capacity). In
    that regime the capacity estimate (max_seq + suffixed over live + recorded
    evicted ids) cannot observe the sequence numbers OR suffix-letters of goals
    evicted before id-tracking existed, so it SYSTEMATICALLY undercounts real
    allocations and FALSE-flags legitimate eviction. The excess is
    capacity-undercount noise, not a resurrection double-count.

    Deliberately does NOT suppress two reliable regimes:
      * evicted_ids populated  -> capacity is observable/tight; trust the check.
      * in_list == capacity (all-live) -> every minted id is live yet census
        claims evictions = impossible; this is the genuine asp-306 resurrection
        signature and stays refused regardless of evicted_ids visibility.

    Loses no reliable detection: the id-intersection resurrection check is
    ALREADY blind on legacy census (no ids to intersect), and every new eviction
    records ids (g-115-2430), so post-fix resurrection is caught by id. Rationale
    + evidence (g-115-2503): .history evicted-id recovery is infeasible (shallow
    copy-on-write snapshots); --repair-census is a wrong-direction treadmill that
    shrinks a CORRECT census to satisfy the undercounted capacity (repaired
    asp-115 to exactly-fit 2026-07-16, regrew +22 in one day)."""
    return not all_evicted_ids(asp) and in_list < capacity


def _conservation_violation(asp: dict):
    """Cross-run conservation canary (; Mechanism D resurrection).

    The in-run _metric_fingerprint assert proves ONE eviction is metric-neutral
    but is blind to cross-run resurrection: a stale write restoring goals[]
    AFTER a census bump leaves the same goals counted twice (asp-306 signature:
    all 87 minted ids in-list + census=8). Evicting from that state re-bumps
    census for the resurrected goals, compounding the double-count — so the
    evictor must REFUSE the aspiration instead.

    Pigeonhole invariant: sequence-minted goals ever allocated cannot exceed
    the id capacity, so  len(sequence goals in list) + census_sum  must be
    <= max minted seq + one extra per suffixed id (each suffix letter is an
    allocation beyond the numeric sequence). Foreign ids (g-xw-*, short test
    ids, other-asp prefixes) sit outside the sequence space — excluded from
    BOTH sides. Returns a violation dict, or None when clean/underivable
    (empty goals[] or no parseable sequence ids: capacity has no observable
    lower bound there, and a false refusal would freeze legitimate eviction).
    """
    goals = asp.get("goals") or []
    # Effective census (legacy counts + evicted-id set, ).
    census_sum = sum(census_by_status(asp).values())
    asp_id = str(asp.get("id", ""))
    asp_num = _asp_num(asp)
    max_seq = 0
    suffixed = 0
    counted = 0
    for g in goals:
        parsed = _parse_seq_id((g or {}).get("id"), asp_num)
        if parsed is None:
            continue
        seq, is_suffixed = parsed
        counted += 1
        max_seq = max(max_seq, seq)
        if is_suffixed:
            suffixed += 1
    # Recorded evicted ids are real minted allocations — count their seqs toward
    # capacity (NOT toward counted: they are census-side, not in-list). Without
    # this, evicting the max-seq goal shrinks the ceiling while the census grows
    # — a built-in false violation. Ids that are ALSO live (resurrected state)
    # are skipped: one allocation, one capacity unit. ()
    live_id_set = {str((g or {}).get("id") or "") for g in goals}
    for gid in all_evicted_ids(asp):
        if gid in live_id_set:
            continue
        parsed = _parse_seq_id(gid, asp_num)
        if parsed is None:
            continue
        seq, is_suffixed = parsed
        max_seq = max(max_seq, seq)
        if is_suffixed:
            suffixed += 1
    if max_seq == 0:
        return None
    capacity = max_seq + suffixed
    if counted + census_sum <= capacity:
        return None
    # : suppress the KNOWN-loose legacy-census false positive (capacity
    # undercounts invisible pre-id-tracking evictions), preserving the all-live
    # resurrection signature. See _legacy_census_loose.
    if _legacy_census_loose(asp, counted, capacity):
        return None
    return {
        "asp_id": asp_id, "goals_in_list": counted, "census_sum": census_sum,
        "max_minted_seq": max_seq, "suffixed_extra": suffixed,
        "capacity": capacity, "excess": counted + census_sum - capacity,
    }


def _warn_violation(v: dict, where: str) -> None:
    """Loud human warning + one machine-readable audit line (stderr)."""
    print(f"[evict] WARN: CONSERVATION VIOLATION on {v['asp_id']} ({where}): "
          f"{v['goals_in_list']} in-list + census {v['census_sum']} exceeds id "
          f"capacity {v['capacity']} (max seq {v['max_minted_seq']}"
          f" + {v['suffixed_extra']} suffixed) by {v['excess']} — SKIPPING this "
          "aspiration (evicting from a resurrected state would re-bump census "
          "and compound the double-count; see tree node "
          "counter-clobber-mechanisms Mechanism D / g-115-1936).",
          file=sys.stderr)
    print("CONSERVATION-VIOLATION " + json.dumps(v, sort_keys=True,
                                                 ensure_ascii=True),
          file=sys.stderr)


def _metric_fingerprint(asp: dict) -> dict:
    """All completion metrics this aspiration influences — must be invariant."""
    fp = {label: effective_counts(asp, exclude_statuses=excl, include_recurring=rec)
          for (excl, rec, label) in _INVARIANT_VARIANTS}
    fp["cadence_completed"] = census_completed(asp) + sum(
        1 for g in (asp.get("goals") or []) if g.get("status") == "completed")
    return fp


def _bump_census(asp: dict, status: str, goal_id) -> None:
    """Record an evicted goal id in archived_census.evicted_ids[status] (sorted,
    deduped). g-115-2430: the id SET replaces the legacy by_status count as the
    census authority — it merges by union (commutative/idempotent, so a stale
    peer can never revert a repair or double it), doubles as the resurrection
    tombstone in coordination_merge._merge_goals, and makes re-evicting a
    resurrected goal a NO-OP (set add), killing the double-count lane of
    g-115-2401. `by_status` is the FROZEN legacy baseline: new evictions never
    touch it; only census repairs shrink it."""
    census = asp.setdefault(CENSUS_KEY, {})
    if not isinstance(census, dict):
        census = {}
        asp[CENSUS_KEY] = census
    ids = census.setdefault("evicted_ids", {})
    if not isinstance(ids, dict):
        ids = {}
        census["evicted_ids"] = ids
    bucket = ids.setdefault(status, [])
    if not isinstance(bucket, list):
        bucket = []
        ids[status] = bucket
    gid = str(goal_id)
    if gid not in bucket:
        bucket.append(gid)
        bucket.sort()


def _capacity(asp) -> int:
    """Pigeonhole ceiling for an aspiration's id-space: the max sequence number
    among live goal ids AND recorded evicted ids (+ count of distinct suffixed
    ids). Ids are minted contiguously from 1, so this is the observable lower
    bound on how many distinct goal ids the aspiration ever allocated. in_list +
    census_sum must not exceed it — if it does, archived_census double-counts
    evicted goals (phantom allocations from evict->resurrect->re-evict cycles).
    Post-g-115-2430, evicted ids are visible via archived_census.evicted_ids and
    counted here (each names a real minted id — and without them, evicting the
    max-seq goal would SHRINK the ceiling while growing the census, a built-in
    false positive). LEGACY count-only census entries still have invisible ids,
    so for those the ceiling stays CONSERVATIVE (maximizes detected excess);
    g-115-1936's independent completions-delta cross-check confirmed that excess
    is genuine phantom, not hidden high-seq eviction. (g-115-1951 / g-115-1938
    conservation canary)"""
    # : reads the grammar through `_parse_seq_id`, the file's ONE
    # parser. This function used to carry its own `rstrip("a-z")` heuristic —
    # the second of four readings — which is what produced the opposite-verdict
    # incident: on  the evict guard `_conservation_violation` (regex,
    # suffix-aware) computed capacity 622 and passed, while `_audit_violations`
    # (this heuristic) computed 618 and reported a 4-goal "CONSERVATION
    # VIOLATION". Same aspiration, same data, opposite verdicts (rb-301), and
    # the audit's remediation is `--repair-census --apply`, which SHRINKS a
    # correct census to satisfy an undercounted ceiling — with
    # `true_evicted_max: -4`, a target `_scale_by_status` turns into a census
    # WIPE, not a shrink.  patched the heuristic; this removes it.
    asp_num = _asp_num(asp)
    live_ids = [str(g.get("id", "")) for g in asp.get("goals", []) or []]
    max_seq = 0
    suffixed = 0
    for gid in dict.fromkeys(live_ids + all_evicted_ids(asp)):  # deduped, ordered
        parsed = _parse_seq_id(gid, asp_num)
        if parsed is None:
            continue
        seq, is_suffixed = parsed
        max_seq = max(max_seq, seq)
        if is_suffixed:
            suffixed += 1
    return max_seq + suffixed


def _audit_violations(items):
    """Read-only pigeonhole conservation audit ( / ).
    Returns a per-aspiration report where in_list_sequence_goals + census_sum
    exceeds id capacity — archived_census claiming more evicted goals than the
    id-space could ever have held. Sorted by excess desc."""
    out = []
    for asp in items:
        # : both sides of the inequality now read one grammar. This
        # counted by naive `startswith(prefix)` while `cap` counted by parser,
        # so a `-a` leg landed on the left side only — the
        # apples-to-oranges that manufactured the phantom EXCESS.
        in_list = _in_list_sequence_goals(asp)
        cap = _capacity(asp)
        census_sum = sum(census_by_status(asp).values())
        claimed = in_list + census_sum
        # : skip the KNOWN-loose legacy-census false positive (see
        # _legacy_census_loose) — the all-live resurrection signature is retained.
        if cap > 0 and claimed > cap and not _legacy_census_loose(asp, in_list, cap):
            out.append({
                "id": asp.get("id", "?"),
                "in_list": in_list,
                "census_sum": census_sum,
                "capacity": cap,
                "excess": claimed - cap,
                "true_evicted_max": cap - in_list,
            })
    out.sort(key=lambda v: -v["excess"])
    return out


def _scale_by_status(bs: dict, old_sum: int, target: int) -> dict:
    """Scale a {status:count} census down to sum==target, preserving proportions
    via largest-remainder rounding (hits target EXACTLY, never over/under by
    rounding). target 0 -> empty. Statuses that round to 0 are dropped."""
    if old_sum <= 0 or target <= 0:
        return {}
    raw = {s: (n * target / old_sum) for s, n in bs.items()}
    floored = {s: int(v) for s, v in raw.items()}
    remainder = target - sum(floored.values())
    # Hand the remaining units to the largest fractional parts first.
    order = sorted(bs.keys(), key=lambda s: -(raw[s] - floored[s]))
    for i in range(remainder):
        floored[order[i % len(order)]] += 1
    return {s: n for s, n in floored.items() if n > 0}


def _make_census_repair(stamp: str):
    """locked_modify_jsonl modifier: for each aspiration with a pigeonhole
    violation, shrink the LEGACY archived_census.by_status counts so
    legacy_sum == capacity - in_list - len(evicted_ids) (the max
    conservation-valid evicted count), preserving per-status proportions and
    stamping a census_note. evicted_ids entries are GROUND TRUTH (each names a
    real evicted goal id) and are never clamped — post-g-115-2430 phantom can
    only live in the legacy counts. Statuses present pre-repair keep an EXPLICIT
    0 (never key-dropped): the cross-box merge treats an absent by_status key as
    no-opinion, so a dropped key would let a stale peer's nonzero resurrect it,
    while an explicit 0 wins the per-status MIN. Non-violating aspirations
    untouched. Returns the modified items; raises if any violation SURVIVES
    (post-repair conservation guard — the write aborts rather than land a
    partial fix)."""
    def _repair(items):
        for asp in items:
            # : same parser as _audit_violations. These two MUST agree
            # on in_list — the audit decides WHETHER to repair and this decides
            # BY HOW MUCH, so a divergence here repairs to a target the
            # post-repair guard below then rejects, aborting the write.
            in_list = _in_list_sequence_goals(asp)
            cap = _capacity(asp)
            census_sum = sum(census_by_status(asp).values())  # legacy + ids
            if cap <= 0 or in_list + census_sum <= cap:
                continue  # no violation — leave untouched
            # : the SAME suppressor `_audit_violations` applies. Found
            # while pinning audit/repair parity: this loop lacked it, so an
            # aspiration the audit deliberately classifies as a KNOWN-LOOSE false
            # positive (`_legacy_census_loose` — capacity provably undercounts
            # pre-id-tracking evictions) was repaired anyway. Reachable, because
            # `main()` gates this whole pass on the audit finding at least one
            # violation ANYWHERE: one genuine violation elsewhere dragged every
            # legacy-loose aspiration into a collateral census shrink the audit
            # had just declared unwarranted. Fewer repairs is the safe direction
            # — the post-repair guard below reads `_audit_violations`, which
            # applies this same suppressor, so a skip here can never leave a
            # survivor.
            if _legacy_census_loose(asp, in_list, cap):
                continue
            census = asp.setdefault(CENSUS_KEY, {})
            if not isinstance(census, dict):
                census = {}
                asp[CENSUS_KEY] = census
            raw_bs = census.get("by_status")
            legacy = {}
            if isinstance(raw_bs, dict):
                for s, n in raw_bs.items():
                    try:
                        legacy[s] = max(0, int(n))
                    except (TypeError, ValueError):
                        continue
            legacy_sum = sum(legacy.values())
            ids_total = sum(len(v) for v in census_evicted_ids(asp).values())
            target = max(0, min(legacy_sum, cap - in_list - ids_total))
            scaled = _scale_by_status(legacy, legacy_sum, target)
            census["by_status"] = {s: scaled.get(s, 0) for s in sorted(legacy)}
            census["census_note"] = (
                f"reconciled g-115-1951 {stamp}: legacy census_sum "
                f"{legacy_sum}->{target} (capacity {cap} - in_list {in_list} - "
                f"{ids_total} evicted ids); per-status proportionally scaled "
                f"from prior distribution; evicted_ids untouched (ground truth).")
        # Post-repair conservation guard: no violation may survive.
        survivors = _audit_violations(items)
        if survivors:
            raise RuntimeError(
                "census repair left violations — ABORTING write: "
                f"{[(v['id'], v['excess']) for v in survivors[:5]]}")
        return items
    return _repair


def _plan(items, cutoff):
    """Return (per_asp_report, total_goals, bytes_freed, violations).
    Aspirations failing the conservation canary are excluded from the plan."""
    report = []
    grand_n = 0
    grand_bytes = 0
    violations = []
    for asp in items:
        v = _conservation_violation(asp)
        if v:
            violations.append(v)
            _warn_violation(v, "plan")
            continue
        n = 0
        freed = 0
        for g in asp.get("goals", []) or []:
            if _eligible(g, cutoff):
                n += 1
                freed += len(json.dumps(g, ensure_ascii=True))
        if n:
            report.append((asp.get("id", "?"), n, freed))
            grand_n += n
            grand_bytes += freed
    report.sort(key=lambda x: -x[1])
    return report, grand_n, grand_bytes, violations


def _make_evictor(cutoff):
    """Build the locked_modify_jsonl modifier_fn (pure in-memory). Moves eligible
    goals out of `goals` into the per-status census and asserts every completion
    metric is invariant before returning; mismatch raises and aborts the write."""
    def _evict(items):
        before = {asp.get("id", f"#{i}"): _metric_fingerprint(asp)
                  for i, asp in enumerate(items)}
        for asp in items:
            v = _conservation_violation(asp)
            if v:
                # Re-checked INSIDE the lock (not just at plan time): the locked
                # read is force-fresh and may see resurrected state the dry-run
                # pass did not. Skipping leaves goals+census untouched, so the
                # before/after fingerprint assert below still holds for this asp.
                _warn_violation(v, "apply")
                continue
            goals = asp.get("goals", []) or []
            kept = []
            for g in goals:
                if _eligible(g, cutoff):
                    _bump_census(asp, g.get("status"), g.get("id"))
                else:
                    kept.append(g)
            asp["goals"] = kept
        after = {asp.get("id", f"#{i}"): _metric_fingerprint(asp)
                 for i, asp in enumerate(items)}
        if before != after:
            # Find the first differing aspiration for a precise diagnostic.
            diffs = [k for k in before if before.get(k) != after.get(k)]
            raise RuntimeError(
                "eviction changed a completion metric — ABORTING write. "
                f"differing aspirations: {diffs[:5]} (of {len(diffs)}). "
                "This is a census-bump bug: effective_counts must read back the "
                "evicted goals from archived_census byte-identically.")
        return items
    return _evict


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("world", "agent"), default="world")
    ap.add_argument("--age-days", type=int, default=45,
                    help="evict goals terminal MORE than this many days ago "
                         "(default 45 — beyond every record-consumer's window)")
    ap.add_argument("--apply", action="store_true",
                    help="perform the write (default: dry-run, read-only)")
    ap.add_argument("--audit", action="store_true",
                    help="read-only pigeonhole conservation audit: report any "
                         "aspiration whose in_list + archived_census exceeds id "
                         "capacity (phantom census double-count). Exit 1 on any "
                         "violation, 0 when clean. (g-115-1951 / g-115-1938)")
    ap.add_argument("--repair-census", action="store_true",
                    help="fix pigeonhole violations: shrink each violating "
                         "aspiration's archived_census to census_sum == capacity "
                         "- in_list (proportional per-status scaling + census_note). "
                         "DRY-RUN unless --apply. Post-repair audit must be clean or "
                         "the write aborts. (g-115-1951)")
    args = ap.parse_args()

    base = WORLD_DIR if args.source == "world" else AGENT_DIR
    if base is None:
        print(f"[evict] {args.source} dir unresolved (WORLD_DIR/AGENT_DIR None "
              "— no MIND_WORLD/MIND_AGENT?)", file=sys.stderr)
        return 2
    path = Path(base) / "aspirations.jsonl"
    if not path.exists():
        print(f"[evict] {path} not found", file=sys.stderr)
        return 2

    cutoff = datetime.now().replace(microsecond=0) - timedelta(days=args.age_days)

    # Read current state for the plan. Under own-cloud, refresh from S3 first so
    # the dry-run reflects exactly what --apply will operate on.
    try:
        from storage_backend import get_backend
        get_backend().refresh(path)
    except Exception as e:
        print(f"[evict] (refresh skipped: {e})", file=sys.stderr)
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]

    # Read-only conservation audit ( / ). Reports and exits
    # BEFORE any eviction plan/write — --audit never mutates.
    if args.audit:
        violations = _audit_violations(items)
        print(f"[audit] source={args.source} path={path}")
        print(f"[audit] scanned {len(items)} aspirations")
        if not violations:
            print("[audit] OK — zero pigeonhole violations "
                  "(census conserves id-space).")
            return 0
        print(f"[audit] {len(violations)} CONSERVATION VIOLATION(S) "
              "(in_list + census_sum > id capacity):")
        total_excess = 0
        for v in violations:
            total_excess += v["excess"]
            print(f"         {v['id']:12} in_list={v['in_list']:4} "
                  f"census={v['census_sum']:4} capacity={v['capacity']:4} "
                  f"EXCESS={v['excess']:4} (true_evicted_max={v['true_evicted_max']})")
        print(f"[audit] total phantom excess: {total_excess}")
        return 1

    # Census repair (): shrink violating archived_census to
    # conservation-valid counts. DRY-RUN unless --apply.
    if args.repair_census:
        violations = _audit_violations(items)
        print(f"[repair] source={args.source} path={path}")
        if not violations:
            print("[repair] nothing to repair — audit already clean.")
            return 0
        print(f"[repair] {len(violations)} violation(s) to reconcile "
              "(census_sum -> capacity - in_list):")
        # Per-asp before/after by_status projection (goal outcome 2 evidence).
        by_id = {str(a.get("id", "")): a for a in items}
        for v in violations:
            asp = by_id.get(v["id"], {})
            before_bs = census_by_status(asp)
            after_bs = _scale_by_status(before_bs, v["census_sum"], v["true_evicted_max"])
            print(f"  {v['id']:12} census_sum {v['census_sum']:4} -> "
                  f"{v['true_evicted_max']:4}  (capacity {v['capacity']}, "
                  f"in_list {v['in_list']}, excess -{v['excess']})")
            print(f"               by_status {dict(before_bs)} -> {after_bs}")
        if not args.apply:
            print("[repair] DRY-RUN — re-run with --apply to write "
                  "(.history snapshots the WHOLE FILE, so this repair is "
                  "roll-back-able as a unit — that is NOT per-record recovery, "
                  "which WHERE THE RECORDS GO retracts as infeasible. "
                  "Post-repair audit must be clean or the write aborts).")
            return 0

        from _fileops import locked_modify_jsonl
        stamp = datetime.now().strftime("%Y-%m-%d")
        locked_modify_jsonl(path, _make_census_repair(stamp))
        try:
            get_backend().refresh(path)
        except Exception as e:
            try:  # report, never raise — see note_swallowed_backend_error ()
                from storage_backend import note_swallowed_backend_error
                note_swallowed_backend_error("refresh", path, e)
            except Exception:
                pass
        after_items = [json.loads(ln) for ln in
                       path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        after_v = _audit_violations(after_items)
        if after_v:
            print(f"[repair] WARN: {len(after_v)} violation(s) REMAIN after "
                  "repair — investigate.", file=sys.stderr)
            return 1
        print(f"[repair] APPLIED — {len(violations)} aspiration(s) reconciled; "
              "audit now clean (zero violations).")
        return 0

    before_bytes = path.stat().st_size
    total_goals_now = sum(len(a.get("goals", []) or []) for a in items)
    report, total_n, freed, violations = _plan(items, cutoff)

    print(f"[evict] source={args.source} path={path}")
    print(f"[evict] current: {before_bytes:,} bytes, {total_goals_now} goals "
          f"across {len(items)} aspirations")
    print(f"[evict] cutoff: terminal before {cutoff.isoformat()} "
          f"(--age-days {args.age_days})")
    print(f"[evict] eligible: {total_n} goals to evict, ~{freed:,} bytes freed")
    if violations:
        print(f"[evict] WARN: {len(violations)} aspiration(s) SKIPPED on "
              f"conservation violation (details on stderr) — resolve the "
              f"resurrection (see counter-clobber-mechanisms Mechanism D) "
              f"before those can be evicted.")
    for asp_id, n, fb in report[:12]:
        print(f"         {asp_id:12} {n:5} goals  ~{fb:,} bytes")

    if total_n == 0:
        print("[evict] nothing to do.")
        return 0

    if not args.apply:
        print(f"[evict] DRY-RUN — no write. Re-run with --apply to evict "
              f"(projected ~{before_bytes - freed:,} bytes after; metrics held "
              f"invariant by census). ONE-WAY: evicted goal RECORDS are not "
              f"re-homed anywhere queryable by id — .history holds a whole-file "
              f"snapshot with no goal-id index, so it can roll the file back but "
              f"cannot answer 'what was g-NNN-NN'. See WHERE THE RECORDS GO.")
        return 0

    # APPLY: locked read-modify-write (DDB lock + If-Match + .history + canary).
    # The modifier asserts every completion metric is invariant or aborts.
    from _fileops import locked_modify_jsonl
    locked_modify_jsonl(path, _make_evictor(cutoff))

    # Post-write validation: re-read fresh, confirm shrink + goal-count drop.
    try:
        get_backend().refresh(path)
    except Exception as e:
        try:  # report, never raise — see note_swallowed_backend_error ()
            from storage_backend import note_swallowed_backend_error
            note_swallowed_backend_error("refresh", path, e)
        except Exception:
            pass
    after_items = [json.loads(ln) for ln in
                   path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    after_bytes = path.stat().st_size
    after_goals = sum(len(a.get("goals", []) or []) for a in after_items)
    evicted = total_goals_now - after_goals
    print(f"[evict] APPLIED: {before_bytes:,} -> {after_bytes:,} bytes "
          f"({100 * (before_bytes - after_bytes) // max(before_bytes, 1)}% smaller); "
          f"{evicted} goals evicted ({total_goals_now} -> {after_goals})")
    if evicted != total_n:
        print(f"[evict] WARN: evicted {evicted} but planned {total_n} — "
              "concurrent write between plan and apply? Re-run dry-run to confirm "
              "state.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
