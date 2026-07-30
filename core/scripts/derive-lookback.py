#!/usr/bin/env python3
"""Derive a recurring audit's lookback window from its ACHIEVED interval.

g-115-4061 / guard-1997. A recurring detective audit that declares an interval
AND hardcodes its own lookback window silently stops covering its own gap the
moment the two diverge. `scorer-override-audit.sh 24` on a goal whose achieved
interval had drifted to 130.5h examined the most recent 24h and NEVER examined
the other ~107h -- not late, not deferred: never -- then printed
`clean (no hits in 24h)`, which reads as an all-clear over the whole gap.

It is self-reinforcing, which is what makes it durable rather than a one-off:
the scorer demotes low-yield recurring goals, demotion lengthens the achieved
interval, a longer interval leaves a larger unexamined window, more findings
fall outside the fixed lookback, more runs print `clean`, and the clean runs
justify deeper demotion. A fixed lookback MANUFACTURES the results that bury it.

THE FIX IS TO DERIVE, NOT TO ENLARGE. guard-1997 rules out both alternatives
explicitly, and they are the two an author reaches for first:
  - Widening the constant (24 -> 168) restores coverage but re-examines every
    event on every run. The filing threshold is a COUNT WITHIN THE WINDOW, so a
    wide window makes breach both likelier and REPEATED -- one real finding
    becomes a filing every single run.
  - Raising the declared interval (24 -> 72) papers over a demotion that is
    working as designed, and opens a FRESH 48h hole against the unchanged
    lookback.
Deriving `max(declared, elapsed_since_last_run)` makes coverage exactly
continuous under arbitrary scheduling drift, with no overlap double-counting,
and self-heals when the scorer's equilibrium moves again.

UNIT-AGNOSTIC BY DESIGN. The ratio is always computed in TIME (elapsed /
declared interval) and then applied to whatever unit the caller's window is in.
That matters because the family is not purely time-based: `alert-sweep.sh
--max 8` passes a literal COUNT which `email-read.sh check-alerts --max N`
turns into "the N newest objects by LastModified" -- a recency window with the
identical silent-tail mechanism in a different dimension. Measured on cc-04
2026-07-30: `--max 8` surfaced 4 alerts where `--max 60` surfaced 33, with a
live `Ayoai (failure): AlertTest` sitting outside the production window.

TWO INVARIANTS, both load-bearing:

1. NEVER NARROWER THAN THE CALLER'S DEFAULT. The derived window is floored at
   `--default`. A derivation that could shrink the window would let a bug here
   make coverage WORSE than the hardcoded constant it replaced -- the one
   outcome that is strictly unacceptable for a fix to a coverage defect.

2. FAIL-OPEN TO THE DEFAULT. Every error path prints `--default` and exits 0.
   That is deliberately the CURRENT (buggy) behaviour: if this helper breaks,
   callers are exactly as covered as they are today, never less. A fail-closed
   helper would take the audit offline, converting a partial-coverage defect
   into a total-coverage one.

Usage:
    py -3 core/scripts/derive-lookback.py --goal-id g-115-2831 --default 24
    py -3 core/scripts/derive-lookback.py --goal-id g-115-817 --default 8 --cap 200
    py -3 core/scripts/derive-lookback.py --goal-id g-115-2831 --default 24 --json

Prints the derived window (an integer) to stdout. Exits 0 always.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
from _runtime_bash import bash_cmd  # noqa: E402

# A goal that has NEVER run has no achieved interval to derive from, so there is
# no ratio to compute. Treat it as maximally uncovered rather than as covered:
# the whole point of the fix is that an unexamined window must not read as clean.
NEVER_RUN_RATIO = 8.0


def _hours_since(iso_ts):
    """Hours between iso_ts and now, or None if unparseable."""
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_ts)[:19])
    except (ValueError, TypeError):
        return None
    return (datetime.now() - dt).total_seconds() / 3600.0


def _read_goal(goal_id, root=None, errors=None):
    """Return the goal record for goal_id, or None.

    `errors` (optional list) collects why each read attempt failed. A caller that
    passes it can tell "the goal genuinely does not exist" apart from "the read
    never succeeded" — those degrade identically (both -> default) but mean
    opposite things, and reporting the wrong one sent this script's own
    fresh-eyes reviewer looking for a missing goal that was never missing.

    Routed through aspirations-read.sh (daemon-backed) rather than a raw read of
    the local aspirations.jsonl. guard-980: on an own-cloud box the local tree is
    a read-through cache, so a raw local read can answer from a stale mirror --
    and a stale mirror here would understate `elapsed` and silently narrow the
    very window this helper exists to widen.
    """
    base = Path(root) if root else PROJECT_ROOT
    for source in ("world", "agent"):
        try:
            # bash_cmd, not a bare ["bash", ...] argv: guard-580 (a bare argv[0]
            # resolves via CreateProcess, which searches System32 before PATH and
            # reaches the WSL stub, hanging forever on a wedged LxssManager) and
            # guard-581 (str(WindowsPath) backslashes are eaten as escapes).
            out = subprocess.run(
                bash_cmd(base / "core/scripts/aspirations-read.sh",
                         "--source", source, "--active"),
                capture_output=True, text=True, cwd=str(base), timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # TimeoutExpired is a SubprocessError, so the 90s timeout lands here
            # too. Record it: a wedged daemon and an absent goal both return
            # None, and only this list can tell them apart.
            if errors is not None:
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
            continue
        if out.returncode != 0 or not out.stdout.strip():
            if errors is not None:
                errors.append(
                    f"{source}: rc={out.returncode}"
                    + (f" stderr={out.stderr.strip()[:120]}" if out.stderr.strip() else " (empty stdout)")
                )
            continue
        try:
            data = json.loads(out.stdout)
        except (ValueError, TypeError) as exc:
            if errors is not None:
                errors.append(f"{source}: unparseable JSON: {exc}")
            continue
        for asp in (data if isinstance(data, list) else [data]):
            if not isinstance(asp, dict):
                continue
            for goal in (asp.get("goals") or []):
                if isinstance(goal, dict) and goal.get("id") == goal_id:
                    return goal
    return None


def derive(goal, default, margin_pct=10.0, cap=None):
    """Compute the derived window. Pure -- no I/O, so it is directly testable.

    Returns (window, detail_dict). `window` is never below `default` and never
    above `cap` when one is given.
    """
    detail = {
        "default": default,
        "margin_pct": margin_pct,
        "cap": cap,
        "declared_interval_h": None,
        "elapsed_h": None,
        "ratio": 1.0,
        "reason": "",
    }

    if not isinstance(goal, dict):
        detail["reason"] = "goal not found — falling back to default (fail-open)"
        return default, detail

    try:
        declared = float(goal.get("interval_hours") or 0) or None
    except (TypeError, ValueError):
        declared = None
    detail["declared_interval_h"] = declared

    last = goal.get("lastAchievedAt")
    elapsed = _hours_since(last)
    detail["elapsed_h"] = round(elapsed, 2) if elapsed is not None else None

    if declared is None or declared <= 0:
        detail["reason"] = "no usable interval_hours — falling back to default (fail-open)"
        return default, detail

    if elapsed is None:
        # Never run (or an unparseable stamp). No ratio exists; widen rather
        # than report clean over a window we cannot bound.
        ratio = NEVER_RUN_RATIO
        detail["reason"] = "never run (or unparseable lastAchievedAt) — widening to NEVER_RUN_RATIO"
    else:
        ratio = max(1.0, elapsed / declared)
        if ratio > 1.0:
            detail["reason"] = (
                f"achieved {elapsed:.1f}h exceeds declared {declared:.1f}h "
                f"— widening by {ratio:.2f}x to cover the gap"
            )
        else:
            # No gap to cover, but the window still gets the margin. That is
            # deliberate OVERLAP, not gap-covering: a window exactly equal to
            # the interval has a zero-width seam at each boundary, and an event
            # landing in the run's own latency falls through it. Say "overlap"
            # rather than "already continuous" — the earlier wording announced
            # continuity while visibly widening, which reads as a bug.
            detail["reason"] = (
                f"achieved {elapsed:.1f}h within declared {declared:.1f}h — on cadence; "
                f"applying {margin_pct:.0f}% boundary overlap only"
            )
    detail["ratio"] = round(ratio, 4)

    window = math.ceil(default * ratio * (1.0 + margin_pct / 100.0))

    # Invariant 1: never narrower than the caller's default.
    window = max(window, default)
    if cap is not None:
        window = min(window, cap)
        # The cap must not be able to violate invariant 1 either. A cap below
        # the default is a caller error; honour the default and say so.
        if window < default:
            window = default
            detail["reason"] += " (cap below default — default honoured)"

    detail["window"] = window
    return window, detail


def main(argv=None):
    ap = argparse.ArgumentParser(description="Derive a recurring audit's lookback from its achieved interval.")
    ap.add_argument("--goal-id", required=True, help="the recurring goal whose cadence drives the window")
    ap.add_argument("--default", type=int, required=True, help="the window the caller would otherwise hardcode")
    ap.add_argument("--margin-pct", type=float, default=10.0, help="overlap margin percent (default 10)")
    ap.add_argument("--cap", type=int, default=None, help="optional upper bound on the derived window")
    ap.add_argument("--json", action="store_true", help="emit the full derivation detail instead of the bare number")
    ap.add_argument("--root", default=None, help="project root override (tests)")
    args = ap.parse_args(argv)

    read_errors = []
    try:
        goal = _read_goal(args.goal_id, root=args.root, errors=read_errors)
        window, detail = derive(goal, args.default, args.margin_pct, args.cap)
        # derive() cannot see WHY the goal is None — it is pure. Correct its
        # "goal not found" reason here when the truth is "the read failed",
        # so a wedged daemon never masquerades as a deleted goal.
        if goal is None and read_errors:
            detail["reason"] = (
                "goal READ FAILED (not absent) — falling back to default (fail-open); "
                + " | ".join(read_errors)
            )
            detail["read_errors"] = read_errors
    except Exception as exc:  # noqa: BLE001 — invariant 2: fail-open to default
        print(f"[derive-lookback] WARN: {exc} — falling back to default {args.default}", file=sys.stderr)
        if args.json:
            print(json.dumps({"window": args.default, "reason": f"exception: {exc}", "default": args.default}))
        else:
            print(args.default)
        return 0

    detail["goal_id"] = args.goal_id
    if args.json:
        print(json.dumps(detail))
    else:
        print(window)
    # ALWAYS emit the line — never gate it on `window != args.default`.
    # Every fail-open path returns EXACTLY the default, so a changed-only gate
    # is silent on precisely the cases that need a signal: a wedged daemon, a
    # timeout, or a missing interval would pin the window to the caller's
    # constant with zero output. That is the same silent-fixed-window condition
    # this script exists to remove, so emitting it here is load-bearing, not
    # chatter. stdout stays the bare number; diagnostics stay on stderr.
    print(
        f"[derive-lookback] {args.goal_id}: {args.default} -> {window} ({detail['reason']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
