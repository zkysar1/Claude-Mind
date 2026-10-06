#!/usr/bin/env python3
"""Owned-redetection recheck markers: the parse + lift predicate for .

WHAT THIS IS. When a detector (a precheck 0.5k ratchet lane, or any repeat
detection) re-fires on a finding whose owner goal is already OPEN and
UNCLAIMED, the stated disposal (precheck SKILL.md 0.5k rows +
core/config/aspirations-precheck-digest.md) is to append ONE short line to the
owner goal's `progress_note` carrying a recheck marker:

    <lane> re-fired [recheck:<agent> <YYYY-MM-DD>]: <one line: what re-fired, that nothing new was found>

instead of composing a full reading paragraph. The marker is the signal a
re-detection happened AT ALL on that day; the line's body is the bounded
counter update. goal-field-append.sh wraps the append with its own
idempotency sentinel `[appended:<marker>]` — pass the marker as
`recheck-<agent>-<YYYYMMDD>` (bare token, no spaces) and put the
recheck line in the TEXT. The idempotency key then means one marker
line per agent per day, which is exactly the counting unit below.

THE TEXT MUST OPEN WITH A WORD, NEVER WITH THE MARKER. On a goal whose
`progress_note` is still empty (a fresh owner) the composed value's first byte
is the text's first byte, and goal-field-append refuses a value that starts
with `[` or `{` (rc=5, guard-6075 cause A; measured 2026-10-04 on g-115-11975,
where the marker-first shape was refused and the same line succeeded once a
word led it). RECHECK_LINE_RE below is unanchored (`finditer`), so the marker
is found wherever it sits in the line; `recheck_marker.py <goal-id>` on that
goal reports the leading-word line as bravo / 2026-10-04.

WHY A MARKER LINE AND NOT A PARAGRAPH. Measured on cc-03 2026-09-30 (omni)
and re-measured on cc-04 2026-10-02 (alpha, this goal): g-115-10165 accumulated
35 appended readings in ~2 weeks — 27,242 chars of re-measured census numbers —
while never being claimed, because NOTHING converted the repeats into work.
A line is ~60 bytes; the note's rotation cap (goal-field-append.py, 32,768 B)
then bounds the pile. The store is append-only under own-cloud commutative
merge (deletion cannot be encoded — goal-field-census-ratchet.py module
docstring), so "update one counter line" means: append one SHORT line; the
predicate below reads the LATEST date per agent, which makes older lines inert
without needing the deletion the store cannot express.

THE LIFT PREDICATE. A goal's `progress_note` carries an OWNED-REDETECTION
lift when at least `min_agents` DISTINCT agents have a recheck marker within
`window_days` of `now` (default 2 agents / 7 days — the goal's own verification
threshold). Distinctness is the whole signal: one agent re-measuring the same
number is the precheck's normal behavior (and the noise this disposal removes);
two independent agents seeing the same unclaimed owner is the "nothing is
taking this" signal. `lift_for` is PURE (note + clock in, verdict out) so the
goal-selector floor and the CLI probe below cannot drift apart.

USAGES:
  py -3 core/scripts/recheck_marker.py <goal-id> [--source world|agent] [--json]
      Read the goal record via aspirations-query.sh (union store, no --source
      flag exists on the query side — g-115-5214; the flag here scopes the
      client-side `source` filter on the per-row key, guard-2588), report the
      recheck state: per-agent latest marker date, distinct agents inside the
      window, and whether the lift is live. Exit 0 always (a report tool);
      exit 2 on an unparseable query.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# The marker line. Agent name is the fleet's lowercase agent vocabulary; the
# date is the day the re-detection was measured (naive UTC, the project
# timestamp posture). Anchored on the literal `[recheck:` prefix so the
# `[appended:recheck-...]` idempotency sentinels never match as markers.
RECHECK_LINE_RE = re.compile(r"\[recheck:([A-Za-z0-9_-]+)[ \t]+(\d{4}-\d{2}-\d{2})\]")

DEFAULT_MIN_AGENTS = 2
DEFAULT_WINDOW_DAYS = 7


def parse_rechecks(note):
    """Parse a progress_note -> {agent: latest marker date}.

    Returns the LATEST date per agent (the store is append-only, so repeats
    accumulate; only the newest per agent carries information — older lines
    age out of the window and are inert). Malformed dates are skipped, not
    raised: a note is prose written by many agents, and one typo must not kill
    the lift for the whole fleet (fail-open, same posture as the selector's
    config loaders).
    """
    latest: dict = {}
    for m in RECHECK_LINE_RE.finditer(note or ""):
        agent = m.group(1).lower()
        try:
            d = date.fromisoformat(m.group(2))
        except ValueError:
            continue
        if d > latest.get(agent, date.min):
            latest[agent] = d
    return latest


def lift_for(note, *, now=None, min_agents=DEFAULT_MIN_AGENTS,
             window_days=DEFAULT_WINDOW_DAYS):
    """Pure lift predicate: does this note carry an owned-redetection lift?

    Returns {"lift": bool, "agents_in_window": [..sorted..],
    "latest": {agent: "YYYY-MM-DD"}}. `lift` is True iff at least `min_agents`
    DISTINCT agents have a marker dated within `window_days` of `now`
    (today's own marker counts: (now - d).days < window_days).
    """
    now = now or date.today()
    latest = parse_rechecks(note)
    in_window = {a: d for a, d in latest.items()
                 if 0 <= (now - d).days < window_days}
    agents = sorted(in_window)
    return {
        "lift": len(agents) >= min_agents,
        "agents_in_window": agents,
        "latest": {a: d.isoformat() for a, d in sorted(latest.items())},
    }


def _query_goal(goal_id, source):
    """Fetch one goal's record via aspirations-query.sh (the only sanctioned
    read path for JSONL stores). Returns the row or None; raises RuntimeError
    on unparseable output (guard-2298: a parse failure is not an empty store)."""
    from _runtime_bash import bash_cmd  # guard-580: never a bare "bash" argv[0]
    proc = subprocess.run(
        bash_cmd((SCRIPT_DIR / "aspirations-query.sh").as_posix(),
                 "--goal-field", "id", goal_id, "--full"),
        capture_output=True, text=True, cwd=str(SCRIPT_DIR.parent.parent))
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        raise RuntimeError(
            f"aspirations-query returned unparseable output for {goal_id!r} "
            f"({len(proc.stdout)} bytes)")
    rows = [r for r in rows if r.get("id") == goal_id or r.get("goal_id") == goal_id]
    if source:
        rows = [r for r in rows if r.get("source") == source]
    return rows[0] if rows else None


def main():
    ap = argparse.ArgumentParser(description="Owned-redetection recheck marker state for one goal (g-115-11721).")
    ap.add_argument("goal_id")
    ap.add_argument("--source", choices=["world", "agent"], default=None,
                    help="Client-side filter on the per-row source key (the query "
                         "wrapper is union-only; g-115-5214).")
    ap.add_argument("--min-agents", type=int, default=DEFAULT_MIN_AGENTS)
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    try:
        row = _query_goal(args.goal_id, args.source)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if row is None:
        print(json.dumps({"goal_id": args.goal_id, "found": False})
              if args.json else f"[recheck-marker] {args.goal_id}: not found")
        return 0

    note = row.get("progress_note") or ""
    state = lift_for(note, min_agents=args.min_agents,
                     window_days=args.window_days)
    result = {
        "goal_id": args.goal_id,
        "found": True,
        "status": row.get("status"),
        "claimed_by": row.get("claimed_by"),
        "claimed_by_sid": row.get("claimed_by_sid"),
        "lift_eligible": bool(state["lift"]) and not row.get("claimed_by"),
        "min_agents": args.min_agents,
        "window_days": args.window_days,
        "agents_in_window": state["agents_in_window"],
        "latest": state["latest"],
        "progress_note_len": len(note),
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"[recheck-marker] {args.goal_id} status={result['status']} "
              f"claimed_by={row.get('claimed_by')}")
        print(f"  latest markers: {state['latest'] or '{}'}")
        print(f"  distinct agents in last {args.window_days}d: "
              f"{len(state['agents_in_window'])} ({', '.join(state['agents_in_window']) or 'none'})")
        print(f"  lift: {'LIVE' if result['lift_eligible'] else 'not live'}"
              + (f" (suppressed: already claimed by {row.get('claimed_by')})"
                 if state["lift"] and row.get("claimed_by") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
