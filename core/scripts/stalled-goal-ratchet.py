#!/usr/bin/env python3
"""Stalled-goal ratchet — how many goals have been NON-EXECUTABLE too long.

THE GAP THIS FILLS. The fleet has a rich family of per-class escape hatches:
the 120h `defer_reason_timeout_hours` fall-through, `handoff-aging-check`
(72h), `dependency-timeout-check`, `blocked-signal-resolution-check`,
`user-blocker-escalation-check`, `precondition-defer-recheck`. Each one asks
"is THIS class of block still valid?" and each answers correctly. None of them
asks the question a human asks: **this goal has not been runnable for three
weeks — is it still worth carrying?**

Measured on the live corpus 2026-09-04 (alpha, DESKTOP-O91DLK2), 129 deferred
goals: median current-defer age 3.6d (healthy), p90 14.2d, max 40.1d. Sixteen
goals were past the 120h TTL with no time gate, not human_blocked, and absent
from the scored set. Their causes spanned FIVE classes — handoff-gated (4),
routed-but-owner-busy (4), dependency chains (4), preconditions (1), plain
defer (1) — plus two carrying `status=blocked` with a NULL clock. Every one was
correctly blocked by its own class's rules. That is the point: correctness per
class does not bound total time, so "forever" is nobody's alarm.

WHY A RATCHET AND NOT A GATE (rb-8533: fixable-danger detectors hard-alarm,
unfixable-debt detectors ratchet). These goals are blocked for real reasons the
fleet cannot dissolve on demand — a human approval, a peer Body's queue, an
unshipped dependency. A gate here would refuse legitimate writes and be
overridden into irrelevance, which is how `post-recovery-edit-gate` nearly
died. A ratchet instead makes the debt VISIBLE and MONOTONE: the count may
shrink freely and never grow silently. That also serves the standing operator
directive to prefer time-to-detection over explanation (`learning-philosophy.md`
§ "Detection outranks attribution").

═══ THE ANTI-LAUNDERING PROPERTY — the load-bearing design choice ═══

Every existing clock ages on a field that the act of re-blocking RESETS:
`cmd_update_goal` re-stamps `defer_reason_set_at` on each new defer_reason. So
a goal re-deferred every four days is permanently young to every sweep, with
no dishonesty required by anyone — each individual defer is fresh and true.

`stall_age_days` therefore takes the **MAXIMUM** age over every available
clock, never the most recent one. `blocked_since` is set once and not
re-stamped, so when a re-defer resets `defer_reason_set_at`, the older
`blocked_since` still wins and the measured stall does not shorten. Rewriting
the reason cannot buy time. `test_rewriting_the_defer_reason_cannot_reset_the_clock`
pins this; it is the reason this module exists rather than another age check.

═══ POPULATION: the selector's own `blocked[]`, never "absent from scored" ═══

The population is exactly the set `goal-selector.sh blocked` reports. It is NOT
"every non-terminal goal missing from the scored set" — that was this module's
first predicate and it was wrong by 66 goals, because the scored set is ONE
agent's vantage and legitimately omits goals that are claimed, routed to
another Body, or recurring-but-not-due. Measured while seeding: 2,418
non-terminal goals, 1,852 scored, 499 in `blocked[]`; the naive predicate put
423 goals in the drift bucket of which only 357 were blocked at all, and the
other 66 were healthy recurring goals like "Check agent email inbox".

The selector owns the definition of non-executable, so this reads it rather
than re-deriving a second one that would drift silently — the guard-1802 class
(a predicate wider or narrower than the gate that creates its population).

═══ `no_clock` — reported, NOT counted, and why that is not a dodge ═══

357 of the 499 blocked goals have no usable timestamp: a goal blocked by an
unmet `blocked_by` while `status` stays `pending` never receives
`blocked_since`, since `cmd_update_goal` sets that field only on a
`status→blocked` write. For those, stall duration is not long or short — it is
UNKNOWN, and folding an unmeasurable 357 into a measurable 22 would swamp the
signal 16:1 and make every future reading unreadable.

So `drift_total` counts only what is measured, and `no_clock` rides in the
breakdown as a COVERAGE figure: how blind the instrument currently is. It is
real drift with a real remedy (give those goals a clock), but it is a different
metric with a different fix, and mixing them would hide both. Worth its own
baseline key once someone owns that backfill; deliberately not claimed here.

═══ `human_blocked:` gets its OWN ratchet — the second anti-laundering move ═══

A `human_blocked:` defer is by design un-clearable by any agent
(`STRUCTURED_DEFER_PREFIXES`, g-115-1646), so folding it into `stalled_goals`
would produce a floor the fleet cannot ratchet down — a permanent `regressed`
verdict that trains readers to ignore the metric.

But simply EXCLUDING it opens the laundering vector this module was built to
close, one level up: rewriting a stalled goal's `defer_reason` to start
`human_blocked:` would drop it out of `drift_total` at no cost, and nothing
would notice. The MAX-clock defends the age; nothing would defend the bucket.

So `human_blocked_goals` is a SECOND baseline key, ratcheting independently in
the same file under the same lock. Relabeling now MOVES debt rather than
deleting it — one counter falls, the other rises, and the rise is a visible
`regressed`. Read the second key as a QUEUE DEPTH for the operator, not a
defect count: a rise means the human's queue grew, which is worth seeing and is
nobody's bug. That is why neither key hard-gates by default.

Read-only with respect to the goal queues: this module never writes a goal. Its
only write is the baseline file, through `locked_modify_yaml`.

Convention: `core/config/conventions/audit-baselines.md`. Population owner:
`reason-less-blocked-check._read_goals` — reused rather than re-derived, so a
second definition cannot drift from it silently (the same rule
`self-blocked-defer-sweep` follows for `audit-deferred-defers.load_deferred`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

METRIC_KEY = "stalled_goals"
# Second key, same file, same lock — see the human_blocked section of the module
# docstring. Its existence is what stops a relabel from erasing debt.
HUMAN_METRIC_KEY = "human_blocked_goals"
DEFAULT_THRESHOLD_DAYS = 14.0

# Terminal statuses never count: a closed goal is not stalled, it is done.
TERMINAL_STATUSES = {
    "completed", "skipped", "expired", "archived", "decomposed", "superseded",
}

# Clocks, in no particular order — every one is consulted and the OLDEST wins.
# Adding a field here can only ever make a stall look LONGER, never shorter,
# which is the safe direction for this metric.
STALL_CLOCK_FIELDS = ("blocked_since", "defer_reason_set_at", "deferred_at")

_HUMAN_BLOCKED_PREFIX = "human_blocked:"


# ─────────────────────────────── pure core ────────────────────────────────
# Everything below to `census()` is pure: no I/O, no clock reads, no globals.
# `now` is always injected so tests pin time exactly.

def _parse_ts(value):
    """Parse an ISO-ish timestamp, tolerating a trailing Z. None on anything else."""
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).strip().replace("Z", ""))
    except (ValueError, TypeError):
        return None


def stall_age_days(goal, now):
    """(age_days, basis) for the OLDEST evidence that this goal stopped running.

    MAX, not most-recent — see the anti-laundering section in the module
    docstring. `basis` names which field produced the age so a reader can tell
    a re-stamped defer from a durable block without re-deriving it.

    Returns (None, "no_clock") when no field yields a usable timestamp: that is
    a finding in its own right, not a missing measurement to paper over.
    """
    best_age = None
    best_basis = "no_clock"
    for field in STALL_CLOCK_FIELDS:
        ts = _parse_ts(goal.get(field))
        if ts is None:
            continue
        age = (now - ts).total_seconds() / 86400.0
        if best_age is None or age > best_age:
            best_age, best_basis = age, field
    return best_age, best_basis


def is_human_blocked(goal):
    return str(goal.get("defer_reason") or "").lstrip().lower().startswith(
        _HUMAN_BLOCKED_PREFIX)


def classify(goal, blocked_ids, now, threshold_days=DEFAULT_THRESHOLD_DAYS):
    """One goal -> one bucket. Order of the checks is the semantics.

    `blocked_ids` is the selector's own `blocked[]` id set — NOT the complement
    of the scored set. See the POPULATION section of the module docstring for
    the 66-goal error that distinction cost on the first draft.

    terminal      — closed; never stalled
    executable    — the selector does not consider it blocked
    human_blocked — non-executable by operator design; reported, not counted
    no_clock      — blocked, but stall duration is UNKNOWN; reported, not counted
    stalled       — blocked, measurably, for longer than the threshold
    young         — blocked, measurably, but not yet long enough to be debt
    """
    if str(goal.get("status") or "").lower() in TERMINAL_STATUSES:
        return "terminal"
    gid = goal.get("id") or goal.get("goal_id")
    if gid not in blocked_ids:
        return "executable"
    if is_human_blocked(goal):
        return "human_blocked"
    age, basis = stall_age_days(goal, now)
    if age is None or basis == "no_clock":
        return "no_clock"
    return "stalled" if age > threshold_days else "young"


def census(goals, blocked_ids, now, threshold_days=DEFAULT_THRESHOLD_DAYS):
    """Bucket every goal and return the ratchet payload.

    `drift_total = stalled` — ONLY the measured, agent-clearable bucket.
    `no_clock` is unmeasurable (357 of 499 blocked goals; folding it in would
    swamp the signal 16:1) and rides in `breakdown` as a coverage figure.
    `human_blocked_total` is returned SEPARATELY and ratchets under its own
    baseline key, so relabeling a stall as `human_blocked:` moves debt between
    two visible counters instead of deleting it.

    Both satisfy the audit-baselines admission rule: non-negative integer counts
    that monotonically improve as items are fixed. A stalled goal leaves its
    bucket when it is unblocked, closed, or re-scoped — never by rewording its
    defer_reason.
    """
    buckets = {k: [] for k in
               ("terminal", "executable", "human_blocked", "no_clock",
                "stalled", "young")}
    for g in goals:
        if not isinstance(g, dict):
            continue
        bucket = classify(g, blocked_ids, now, threshold_days)
        age, basis = stall_age_days(g, now)
        buckets[bucket].append({
            "goal_id": g.get("id") or g.get("goal_id"),
            "age_days": None if age is None else round(age, 1),
            "basis": basis,
            "status": g.get("status"),
            "source": g.get("_source"),
            "title": (g.get("title") or "")[:90],
        })
    # Oldest first — a reader triaging this list wants the worst case first.
    for rows in buckets.values():
        rows.sort(key=lambda r: (r["age_days"] is None, -(r["age_days"] or 0.0)))
    return {
        "drift_total": len(buckets["stalled"]),
        "human_blocked_total": len(buckets["human_blocked"]),
        "threshold_days": threshold_days,
        "breakdown": {k: len(v) for k, v in buckets.items()},
        "rows": {"stalled": buckets["stalled"], "no_clock": buckets["no_clock"],
                 "human_blocked": buckets["human_blocked"]},
        "scanned": sum(len(v) for v in buckets.values()),
    }


# ──────────────────────────────── I/O half ─────────────────────────────────

def _load_population():
    """Reuse reason-less-blocked-check._read_goals — do NOT re-derive it.

    That function owns "all active goals in a source", routes through the
    daemon, and is guard-383 fatal on a per-source read error (a silent
    `return []` in an N>=2 aggregator writes a complete-looking lie). A second
    definition here would drift from it with nothing to say which was right —
    the rule self-blocked-defer-sweep follows for audit-deferred-defers.
    Hyphenated filename, hence importlib.
    """
    path = SCRIPT_DIR / "reason-less-blocked-check.py"
    spec = importlib.util.spec_from_file_location("_rlbc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    goals = []
    for source in ("world", "agent"):
        goals.extend(mod._read_goals(source))
    return goals


def _blocked_ids():
    """Ids the SELECTOR calls blocked — the population owner.

    Deliberately NOT the complement of `goal-selector.sh select`: that set is
    one agent's vantage and legitimately omits claimed, cross-routed and
    not-yet-due recurring goals (the 66-goal error in the POPULATION section).
    `blocked` is the selector's own answer to "which goals cannot run", so
    reading it keeps this module's predicate identical to the one that creates
    its population instead of a second definition free to drift (guard-1802).

    An empty set is NOT a valid reading — see the refuse-to-seed guard in
    main(). A live fleet always has some blocked goals; zero means the probe
    failed, and seeding a 0 baseline off a failed probe would mark every future
    real reading `regressed` forever.
    """
    from _runtime_bash import bash_cmd  # noqa: WPS433 — local, mirrors siblings
    proc = subprocess.run(
        bash_cmd("core/scripts/goal-selector.sh", "blocked"),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return set()
    rows = data if isinstance(data, list) else (
        data.get("blocked_goals") or data.get("goals") or [])
    return {r.get("goal_id") or r.get("id")
            for r in rows if isinstance(r, dict)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute and report without touching the baseline file")
    ap.add_argument("--json", action="store_true", help="Emit the full census as JSON")
    ap.add_argument("--threshold-days", type=float, default=DEFAULT_THRESHOLD_DAYS)
    args = ap.parse_args(argv)

    hard_gate = str(__import__("os").environ.get(
        "VERIFY_LEARNING_DRIFT_HARD_GATE", "")).strip() == "1"

    goals = _load_population()
    blocked = _blocked_ids()

    # REFUSE TO SEED FROM AN UNREADABLE WORLD (the sibling ratchets' guard).
    # Both emptinesses mean "the instrument is broken", never "the drift
    # changed" — and they fail in OPPOSITE directions, which is why each needs
    # its own refusal rather than one shared check. An empty population would
    # report drift 0 (a floor nothing can beat); an empty blocked[] would too,
    # by making every goal read `executable`. Seeding either would poison the
    # baseline permanently, since the baseline only ever shrinks.
    if not goals:
        print("REFUSED: goal population empty — the store is unreachable or "
              "empty; refusing to compute a baseline against it", file=sys.stderr)
        return 1 if hard_gate else 0
    if not blocked:
        print("REFUSED: goal-selector reported ZERO blocked goals — on a live "
              "fleet that is a probe failure, not a clean queue. Refusing to "
              "seed a 0 baseline from it.", file=sys.stderr)
        return 1 if hard_gate else 0

    now = dt.datetime.now()
    result = census(goals, blocked, now, args.threshold_days)

    if args.json:
        print(json.dumps(result, indent=2))

    verdicts = {METRIC_KEY: "dry-run", HUMAN_METRIC_KEY: "dry-run"}
    if not args.dry_run:
        from _fileops import locked_modify_yaml  # noqa: WPS433
        from _paths import META_DIR  # noqa: WPS433
        baselines_path = Path(META_DIR) / "audit-baselines.yaml"
        stamp = now.isoformat(timespec="seconds")
        counts = {METRIC_KEY: result["drift_total"],
                  HUMAN_METRIC_KEY: result["human_blocked_total"]}
        box = {}

        def _ratchet_one(baselines, key, current):
            """One metric's read-compare-write. Shared so the two keys cannot
            drift apart in their verdict semantics."""
            entry = baselines.get(key) or {}
            prior = entry.get("baseline")
            if prior is None:
                v = "seeded"
                entry["baseline"] = current
            elif current < prior:
                v = "ratcheted"
                entry["baseline"] = current      # one-way: shrink only
            elif current == prior:
                v = "stable"
            else:
                v = "regressed"                  # baseline deliberately NOT raised
            entry["last_recorded"] = stamp
            entry["last_verdict"] = v
            history = list(entry.get("history") or [])
            history.append({"recorded_at": stamp, "drift_total": current,
                            "verdict": v, "breakdown": result["breakdown"]})
            entry["history"] = history[-50:]     # bounded, per the convention
            baselines[key] = entry
            box[key] = v

        def _modify(baselines):
            # Read the prior baselines INSIDE the lock: sibling ratchets share
            # this file, so a read outside would race and one writer's update
            # would revert the other's. Both keys are written in ONE pass so a
            # relabel can never land as half a move (stalled down, human_blocked
            # not yet up) — which is exactly the window the laundering vector
            # would need.
            if not isinstance(baselines, dict):
                baselines = {}
            for key, current in counts.items():
                _ratchet_one(baselines, key, current)
            return baselines

        locked_modify_yaml(baselines_path, _modify)
        verdicts.update(box)

    # The denominator is printed on purpose: `stalled=22` alone is unreadable
    # without knowing how many goals were even eligible, and `no_clock` is the
    # instrument's blind spot — a reader who cannot see it cannot tell a real
    # improvement from a coverage collapse.
    b = result["breakdown"]
    blocked_pop = (b["stalled"] + b["young"] + b["no_clock"] + b["human_blocked"])
    # Under --json the summary goes to STDERR: stdout must stay parseable JSON
    # or the flag is a trap for every caller that pipes it.
    print("%s: %s=%d (blocked >%.0fd, of %d blocked) | %s: %s=%d "
          "[not counted: no_clock=%d, young=%d] scanned=%d"
          % (str(verdicts[METRIC_KEY]).upper(), METRIC_KEY,
             result["drift_total"], result["threshold_days"], blocked_pop,
             str(verdicts[HUMAN_METRIC_KEY]).upper(), HUMAN_METRIC_KEY,
             result["human_blocked_total"],
             b["no_clock"], b["young"], result["scanned"]),
          file=sys.stderr if args.json else sys.stdout)
    if hard_gate and "regressed" in verdicts.values():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
