"""Bounded LLM-facing projection of the aspirations compact ().

WHY THIS IS A MODULE AND NOT A HEREDOC. This projection used to live as an
inline `python3 -c` block inside load-aspirations-compact.sh. That is why it
went 2.5x over the Read-tool cap unnoticed for months: a heredoc cannot be
imported, so it cannot be unit-tested, so no regression pin could exist. The
goal that motivated this file asks for exactly such a pin (item d), and the
extraction is the minimum change that makes one possible. Two call sites --
the loader and tests/test_compact_summary_budget.py -- so this is not a
single-use abstraction.

THE DEFECT. The old projection dropped completed-non-recurring goals and kept
15 fields, and that was the whole bound. A per-goal projection over a
monotonically growing store has no upper bound at all, so it grew with the
corpus: measured 2026-08-11 on cc-07, 672,651 bytes against a 262,144-byte
Read-tool cap -- 2.57x over, and the caller contract ("IF path returned: Read
it") was therefore unsatisfiable end-to-end.

WHY FIELD-TRIMMING ALONE CANNOT FIX IT -- measured, not assumed, because the
motivating goal explicitly warns against assuming field bloat. Byte census of
the 672 KB over 2,249 goals: title 42.3%, category 12.2%, participants 9.0%,
defer_reason 8.7% (on only 90 rows, avg 626 B), status 7.2%, priority 7.1%,
id 6.5%. Keeping ONLY id+title+status is still 360,889 B = 1.38x the cap. And
excluding every terminal status on top of that reaches 1.99x. Both levers
together do not fit; the population is the problem, so the bound has to be a
BYTE BUDGET over rows.

THE BOUND COMES FROM THE CAP, NOT FROM TODAY'S CORPUS. That is the point of
BUDGET_FRACTION: as the store grows, more rows are omitted and the file stays
under the cap. A constant tuned to today's row count would re-breach on the
next thousand goals, which is precisely how the original got here.

NOTHING IS DROPPED SILENTLY. Every aspiration that loses goals carries an
inline `goals_omitted` count, and build_summary returns a stats dict the
loader prints to stderr. A projection that quietly truncates reads as a
complete portfolio, which is worse than one that is loudly partial.

CONSUMER SAFETY -- verified before trimming, because the co-filed addendum on
g-115-5824 warns that a smaller projection can make consumer breakage WORSE.
Grepped the whole repo: the ONLY files referencing this artifact are the
loader, context-reads.py (tracking), session-manifest.yaml (registry) and its
test. Every PYTHON consumer -- precheck-eval.py, obligation-audit.py,
findings-gate.py -- reads the FULL aspirations-compact.json instead. So the
summary's only consumer is an LLM Read in skill pseudocode, and the tiering
below is scoped to keep what that pseudocode actually filters on.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The canonical terminal set. Imported from _goal_census rather than re-typed:
# that module is a deliberate LEAF (it imports nothing from aspirations or
# goal-selector, so this stays cheap on the loader's hot path) and its literal
# is already drift-guarded against aspirations.TERMINAL_GOAL_STATUSES by
# tests/test_goal_eviction_invariance.py. Adding a third copy here would fork
# the vocabulary; note in particular that `retired` is NOT terminal in the
# SSOT, and this module must not invent it as one.
from _goal_census import TERMINAL_STATUSES

# The Read tool's hard refusal threshold, in bytes. A file at or above this
# cannot be read by the LLM at all -- which is the failure this module exists
# to prevent, not merely an efficiency concern.
READ_TOOL_CAP = 262144

# Headroom below the cap. 0.75 leaves a quarter of the budget for growth
# between regenerations and for the encoding slack of non-ASCII escapes. The
# always-keep tier measured 146,123 B (0.56x cap) on the corpus that motivated
# this, so this fraction admits every must-keep row plus all HIGH-priority
# pending work with room to spare.
BUDGET_FRACTION = 0.75

# Fields carried per goal. `achievedCount` was ADDED here (
# addendum): strategic-scan Phase S1 gates its candidate sensors on
# `g.get('achievedCount', 0) >= 2` against this projection, and the field was
# absent from every row, so S1 selected 0 of 82 recurring goals and had never
# emitted a signal. It costs ~18 bytes on the ~77 rows that carry it.
SUMMARY_KEEP = {
    'id', 'title', 'status', 'priority', 'category', 'skill', 'recurring',
    'interval_hours', 'lastAchievedAt', 'achievedCount', 'participants',
    'blocked_by', 'deferred_until', 'defer_reason', 'depends_on', 'started',
}

# defer_reason is narrative and was the worst byte-per-row offender in the
# census (avg 626 B on 90 rows, 8.7% of the whole file). Truncation is visible
# (the ellipsis) and lossless where it matters: the reclaim sweeps that reason
# about defer text read the full store, never this projection.
DEFER_REASON_MAX = 120

# Tier order decides what survives the budget. Lower survives first.
#   0  recurring / in-progress / blocked -- the rows LLM consumers filter on.
#      Recurring is tier 0 unconditionally BECAUSE strategic-scan S1 surveys
#      it; dropping recurring rows to fit would silently re-break S1 the same
#      way the missing achievedCount did.
#   1..3  pending, by priority.
TIER_ALWAYS = 0
_PRIORITY_TIER = {'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}

# Bytes reserved per aspiration for a possible `,"goals_omitted":NNNNN` key.
# 24 covers a 6-digit count, which is far past any plausible queue size.
_OMITTED_KEY_RESERVE = 24
_TIER_LABELS = {
    0: 'always (recurring/in-progress/blocked)',
    1: 'pending-HIGH',
    2: 'pending-MEDIUM',
    3: 'pending-LOW',
}


def _encoded_len(obj):
    """Byte length of obj as this module serializes it.

    Measured with the SAME separators/ensure_ascii the writer uses, so the
    budget arithmetic cannot disagree with the file that lands on disk.
    """
    return len(json.dumps(obj, separators=(',', ':'),
                          ensure_ascii=True).encode('utf-8'))


def project_goal(goal):
    """Field-narrow one goal and truncate its narrative field."""
    out = {k: v for k, v in goal.items() if k in SUMMARY_KEEP}
    reason = out.get('defer_reason')
    if isinstance(reason, str) and len(reason) > DEFER_REASON_MAX:
        out['defer_reason'] = reason[:DEFER_REASON_MAX] + '...'
    return out


def goal_tier(goal):
    """Tier for budget ordering, or None when the goal is excluded outright.

    A recurring goal is NEVER excluded on status: a recurring goal cycles
    through `completed` between fires, so a status-only exclusion would drop
    the entire recurring population at whatever moment the cycle caught it.
    That is the same trap the original projection avoided for `completed` and
    this one must preserve for every terminal status it added.
    """
    if goal.get('recurring'):
        return TIER_ALWAYS
    if goal.get('status') in TERMINAL_STATUSES:
        return None
    if goal.get('status') in ('in-progress', 'blocked'):
        return TIER_ALWAYS
    priority = (goal.get('priority') or 'MEDIUM').upper()
    return _PRIORITY_TIER.get(priority, _PRIORITY_TIER['MEDIUM'])


def build_summary(merged, budget_bytes=None):
    """Build the bounded projection. Returns (summary, stats).

    `summary` keeps the TOP-LEVEL ARRAY-OF-ASPIRATIONS shape the existing LLM
    pseudocode iterates (`for asp in compact for g in asp.goals`). The
    truncation record is an inline per-aspiration `goals_omitted` key rather
    than a wrapper object precisely so that shape does not change -- a wrapper
    would break every consumer at once to report that some rows are missing.
    """
    if budget_bytes is None:
        budget_bytes = int(READ_TOOL_CAP * BUDGET_FRACTION)

    shells = []
    rows = []
    for index, asp in enumerate(merged):
        shells.append({k: v for k, v in asp.items() if k != 'goals'})
        for goal in asp.get('goals', []):
            tier = goal_tier(goal)
            if tier is None:
                continue
            rows.append((tier, index, project_goal(goal)))

    # Budget the SHELLS first: aspiration-level fields are what
    # fresh-eyes completion_health and strategic-scan S3/S4a read, and they are
    # a small fixed cost that must never be crowded out by goal rows.
    spent = _encoded_len([dict(s, goals=[]) for s in shells])

    # Reserve for the `goals_omitted` keys BEFORE budgeting rows. Those keys are
    # written after the loop, so budgeting without them lets the file land over
    # budget by exactly their weight -- measured 310 B over on the first live
    # run of this module. Whether an aspiration needs one is not knowable until
    # the loop finishes, so reserve for all of them: the over-reservation is
    # bounded by the aspiration count (tens of bytes), while the alternative is
    # an invariant that is quietly false.
    spent += _OMITTED_KEY_RESERVE * len(shells)
    kept = [[] for _ in shells]
    omitted = [0 for _ in shells]

    # Stable order: tier first, then original position, so the projection is
    # deterministic across runs on an unchanged store. A projection that
    # reshuffles on every regeneration defeats the context-reads dedup.
    rows.sort(key=lambda r: (r[0], r[1]))

    dropped_by_tier = {}
    for tier, index, goal in rows:
        cost = _encoded_len(goal) + 1  # +1 for the array comma
        if spent + cost > budget_bytes:
            omitted[index] += 1
            label = _TIER_LABELS.get(tier, str(tier))
            dropped_by_tier[label] = dropped_by_tier.get(label, 0) + 1
            continue
        spent += cost
        kept[index].append(goal)

    summary = []
    for index, shell in enumerate(shells):
        entry = dict(shell)
        entry['goals'] = kept[index]
        if omitted[index]:
            # Inline and unmissable: a reader looking at this aspiration sees
            # that it is partial without having to consult anything else.
            entry['goals_omitted'] = omitted[index]
        summary.append(entry)

    total_goals = sum(len(a.get('goals', [])) for a in merged)
    stats = {
        'budget_bytes': budget_bytes,
        'read_tool_cap': READ_TOOL_CAP,
        'goals_total': total_goals,
        'goals_eligible': len(rows),
        'goals_terminal_excluded': total_goals - len(rows),
        'goals_included': sum(len(g) for g in kept),
        'goals_omitted_for_budget': sum(omitted),
        'dropped_by_tier': dropped_by_tier,
    }
    return summary, stats
