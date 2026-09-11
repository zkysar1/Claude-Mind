#!/usr/bin/env python3
"""Inverted-Window Check — flag hypothesis-resolution goals whose
`deferred_until` was taken from `resolves_by` instead of
`resolves_no_earlier_than`, freezing the goal for its ENTIRE resolution window
so it becomes selectable only on the day it expires.

THE RULE THIS ENFORCES is guard-3208: the two dates BRACKET a window —
`resolves_no_earlier_than` is when the goal becomes workable, `resolves_by` is
its deadline. Setting the time gate to the deadline inverts the bracket.

WHY A NEW SIBLING RATHER THAN A CLAUSE ON defer-drift-check.py (guard-3628 —
"the sweep failed to catch it" and "the sweep decided not to" are different
findings). defer-drift-check.py requires `deferred_until` to be PAST *and* a
STRUCTURED defer prefix on `defer_reason`. An inverted window is the opposite
shape on both axes: the gate is in the FUTURE, and there is usually no
`defer_reason` at all — which is exactly what guard-3208's own text predicts
("every sweep that looks for a stale defer_reason misses it because there IS no
defer_reason -- only a deferred_until"). Bolting this predicate onto that sweep
would break its stated complement relationship with precondition-defer-recheck.py
rather than extend it.

MEASURED BEFORE THIS SWEEP EXISTED (alpha worker Body, hostname cc-08,
uname -r 6.8.0-139-generic, 2026-09-10, g-335-1550). Over 2,577 non-terminal
goals: 26 carried `deferred_until`, 12 carried both window fields, and 9 of
those 12 were inverted — including one frozen 89 days (g-350-216: window opened
2026-08-15, gate 2026-11-12) and two frozen since 2026-08-08. Five existing
sweeps (defer-drift-check, defer-recheck, precondition-defer-recheck,
audit-deferred-defers, self-blocked-defer-sweep) all read clean over the
canonical instance in one iteration. This is guard-3208's second and third
measured cohort; its first (g-250-337) froze 13 days and its second
(g-335-1110) froze the full 29.

TWO BUCKETS, DELIBERATELY NOT MERGED.

  inverted[]  — the HIGH-PRECISION copy signature, and the only count that
                should drive action:
                  status non-terminal
                  AND deferred_until, resolves_no_earlier_than, resolves_by all
                      present and parseable
                  AND resolves_no_earlier_than < deferred_until   (the window
                      genuinely opened before the gate — a same-day window whose
                      gate equals both dates is CORRECT, not inverted)
                  AND deferred_until date == resolves_by date     (the copy)

  wide_gate[] — ADVISORY ONLY, never counted as inverted: deferred_until >
                resolves_no_earlier_than without the copy signature. A later
                defer for an unrelated reason produces this shape legitimately,
                so it is reported for a reader and never treated as a defect.
                guard-3208's action_hint offers this as the "more general"
                predicate; measured here it admits one arguable row the exact
                predicate correctly declines, which is why the split exists.

DETECTIVE, NOT CORRECTIVE — no `--apply`, no mutation path. The correct re-gate
date is a judgment (clear the gate outright when the window has opened, or move
it to `resolves_no_earlier_than`), and a sweep that guesses it would surface
genuinely-not-ready goals. Detection is the hard part; the report is the
deliverable. Same contract as defer-drift-check.py.

COVERAGE, stated honestly (pre-execution.md Step 5): this closes the DETECTION
gap completely — it reads the goal store, so it sees an inverted window whatever
code path wrote it. It closes NONE of the PREVENTION gap: nothing here stops a
writer setting the field wrong. The write side is owned by g-115-4909, whose
progress_note carries the same finding.

Guards honored: guard-383 (per-source read error fatal — a silent `return []`
would write a complete-looking "0 inverted" lie), guard-420 (tolerant datetime),
guard-614 (structured JSON output), guard-645 (every field read with a default),
guard-2298 (the unfiltered population is reported beside the filtered count),
guard-3628 (the advisory bucket is separated, not folded in), guard-2448 (the
predicate was run against 9 real rows through this exact data path before the
sweep was written). Detective template: defer-drift-check.py,
reason-less-blocked-check.py. Reference: g-335-1550, guard-3208, g-115-4909.
"""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client (post-cutover; see _rt.py)

NON_TERMINAL = ("pending", "in-progress", "blocked")


def _parse(value):
    """Tolerant ISO parse -> datetime, or None (guard-420).

    Accepts both shapes the store carries in these fields: a bare date
    ("2026-09-01") and a full timestamp ("2026-10-15T00:00:00"). Returns None on
    anything else — an unparseable date must never manufacture a flag.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt, width in (("%Y-%m-%dT%H:%M:%S", 19),
                       ("%Y-%m-%d %H:%M:%S", 19),
                       ("%Y-%m-%d", 10)):
        try:
            return dt.datetime.strptime(text[:width], fmt)
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def classify(goal):
    """Pure classification of ONE goal (no I/O — unit-testable).

    Returns "inverted", "wide_gate", or None. See the module docstring for why
    the two buckets are kept apart.
    """
    if not isinstance(goal, dict):
        return None
    if goal.get("status") not in NON_TERMINAL:
        return None
    du = _parse(goal.get("deferred_until"))
    rne = _parse(goal.get("resolves_no_earlier_than"))
    if du is None or rne is None:
        return None
    if du <= rne:
        return None  # gate at or before the window opening — correct
    rby = _parse(goal.get("resolves_by"))
    if rby is not None and du.date() == rby.date():
        return "inverted"
    return "wide_gate"


def _read_goals(source):
    """Read goals from world or agent queue via the daemon.

    guard-383 fatal symmetry (rb-987): a per-source read error in an N>=2 source
    aggregator MUST be fatal — a silent `return []` writes a complete-looking lie
    into the merged aggregate. The single fail-open boundary is the shell
    wrapper. Mirrors defer-drift-check.py / reason-less-blocked-check.py.
    """
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[inverted-window-check] {source} read failed: {e.body or e}",
              file=sys.stderr)
        sys.exit(1)  # guard-383: source error fatal
    data = _rt.tolerant_decode_aggregate(f"inverted-window-check: {source}", out)
    if data is None:
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            g["_source"] = source
            g["_aspiration_id"] = asp.get("id")
            goals.append(g)
    return goals


def _entry(goal, now):
    du = _parse(goal.get("deferred_until"))
    rne = _parse(goal.get("resolves_no_earlier_than"))
    return {
        "goal_id": goal.get("id"),
        "source": goal.get("_source"),
        "aspiration_id": goal.get("_aspiration_id"),
        "status": goal.get("status"),
        "deferred_until": goal.get("deferred_until"),
        "resolves_no_earlier_than": goal.get("resolves_no_earlier_than"),
        "resolves_by": goal.get("resolves_by"),
        "frozen_days": (du - rne).days if (du and rne) else None,
        "still_frozen": bool(du and du > now),
        "title": (goal.get("title") or "")[:80],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=("Flag non-terminal goals whose deferred_until was taken "
                     "from resolves_by instead of resolves_no_earlier_than, "
                     "freezing the resolution window (guard-3208). Detective "
                     "only — never mutates. Reference: g-335-1550."))
    ap.add_argument("--output", choices=["json", "human"], default="json")
    args = ap.parse_args(argv)

    now = dt.datetime.now()
    all_goals = _read_goals("world") + _read_goals("agent")

    non_terminal = [g for g in all_goals if g.get("status") in NON_TERMINAL]
    inverted, wide_gate = [], []
    for g in non_terminal:
        verdict = classify(g)
        if verdict == "inverted":
            inverted.append(_entry(g, now))
        elif verdict == "wide_gate":
            wide_gate.append(_entry(g, now))

    inverted.sort(key=lambda e: (e.get("goal_id") or ""))
    wide_gate.sort(key=lambda e: (e.get("goal_id") or ""))

    # guard-2298: the unfiltered population travels WITH the filtered count, so
    # a zero can be read against the denominator that produced it.
    result = {
        "scanned": len(all_goals),
        "non_terminal": len(non_terminal),
        "carry_deferred_until": sum(
            1 for g in non_terminal if g.get("deferred_until")),
        "carry_both_window_fields": sum(
            1 for g in non_terminal
            if g.get("deferred_until") and g.get("resolves_no_earlier_than")),
        "inverted_count": len(inverted),
        "inverted": inverted,
        "wide_gate_count": len(wide_gate),
        "wide_gate": wide_gate,
        "still_frozen_count": sum(1 for e in inverted if e["still_frozen"]),
        "now": now.isoformat(timespec="seconds"),
    }

    if args.output == "human":
        print(f"scanned={result['scanned']} "
              f"non_terminal={result['non_terminal']} "
              f"carry_both={result['carry_both_window_fields']} "
              f"inverted={result['inverted_count']} "
              f"still_frozen={result['still_frozen_count']} "
              f"wide_gate={result['wide_gate_count']}")
        for e in inverted:
            print(f"  [inverted] {e['goal_id']} ({e['source']}) "
                  f"rne={e['resolves_no_earlier_than']} -> "
                  f"deferred_until={e['deferred_until']} "
                  f"(== resolves_by) frozen {e['frozen_days']}d: {e['title']}")
        for e in wide_gate:
            print(f"  [wide-gate/advisory] {e['goal_id']} ({e['source']}) "
                  f"rne={e['resolves_no_earlier_than']} -> "
                  f"deferred_until={e['deferred_until']}: {e['title']}")
    else:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
