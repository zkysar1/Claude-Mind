#!/usr/bin/env python3
"""close-owed-sentinel.py — pure helpers for the pending_close_owed WM sentinel ().

Problem: `iteration-close.sh --phase verify` marks a deep, non-recurring goal
completed BEFORE its spark, state-update and learning-gate run. A compaction in
that window leaves nothing durable that names the owed steps, so the loop
re-selects and the goal ends completed with no encoding. Why a sentinel, why it
clears at state-update and not at the spark, and what it deliberately does not
cover: core/config/rationale/owed-close-sentinel.md.

The sentinel is one payload in one WM slot, written by do_verify and cleared by
do_state_update. The entry battery (orchestrator-entry-battery.py) lists it.

PURE stdin->stdout — no wm I/O here. A python process must not spawn `bash wm-*.sh`
(the python->bash subprocess hang, rb-225 / rb-247), so iteration-close.sh does the
wm reads and writes and pipes the slot value THROUGH this script, exactly as it
does for spark-fire-dedup.py:

    payload:      close-owed-sentinel.py payload --goal G --source S --outcome O --started-at T
    check-write:  wm-read.sh pending_close_owed --json | close-owed-sentinel.py check-write --goal G
    check-clear:  wm-read.sh pending_close_owed --json | close-owed-sentinel.py check-clear --goal G

Verdicts (one line on stdout, rc always 0 for the check-* commands):
    check-write   write | refuse:<other-goal-id>
    check-clear   clear | keep:<other-goal-id> | absent

The slot carries a payload derived from THIS close, so it is loss-bearing
(guard-2104): an unconditional write would cancel another goal's unconsumed
obligation with nothing reporting it. check-write therefore refuses to overwrite
a payload that names a different goal, and check-clear removes only its own.

Fail-open in the direction that loses nothing: a helper error on check-write
means write, on check-clear means leave the slot (a stuck sentinel nags and has a
manual clear; a wrongly cleared one drops the obligation silently).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

SLOT = "pending_close_owed"  # documented for callers; this script is slot-agnostic
PRODUCER = "nonrecurring-verify"
OWED_STEPS = ["spark", "state-update", "learning-gate"]
SUMMARY_CAP = 240


def build_payload(goal_id, source, outcome, started_at, summary="", now=None):
    """The sentinel payload. `started_at` is when the verify phase began, so a reader
    can judge whether a close process could still be running (it must still check
    process state, which the diary and this slot cannot report)."""
    return {
        "goal_id": goal_id,
        "source": source,
        "outcome": outcome,
        "producer": PRODUCER,
        "started_at": started_at,
        "set_at": now or datetime.now().isoformat(timespec="seconds"),
        "owed": list(OWED_STEPS),
        "summary": (summary or "")[:SUMMARY_CAP],
    }


def slot_payload(raw):
    """The dict a `wm-read.sh <slot> --json` read returned, or None when the slot is
    empty, null, unparseable or not an object."""
    text = (raw or "").strip()
    if not text or text == "null":
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def write_verdict(raw, goal_id):
    """`write`, or `refuse:<other>` when the slot already holds another goal's obligation."""
    held = slot_payload(raw)
    other = (held or {}).get("goal_id")
    if other and other != goal_id:
        return f"refuse:{other}"
    return "write"


def clear_verdict(raw, goal_id):
    """`clear` only when the slot names THIS goal; `keep:<other>` leaves another goal's
    obligation intact; `absent` when there is nothing readable to clear."""
    held = slot_payload(raw)
    if held is None:
        return "absent"
    other = held.get("goal_id")
    if other == goal_id:
        return "clear"
    return f"keep:{other or 'unattributed'}"


def cmd_payload(args):
    sys.stdout.write(json.dumps(
        build_payload(args.goal, args.source, args.outcome, args.started_at, args.summary)))
    return 0


def cmd_check_write(args):
    sys.stdout.write(write_verdict(sys.stdin.read(), args.goal))
    return 0


def cmd_check_clear(args):
    sys.stdout.write(clear_verdict(sys.stdin.read(), args.goal))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="pending_close_owed sentinel helpers (g-115-11905)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("payload", help="stdout=the sentinel payload JSON")
    pp.add_argument("--goal", required=True)
    pp.add_argument("--source", required=True)
    pp.add_argument("--outcome", required=True)
    pp.add_argument("--started-at", required=True)
    pp.add_argument("--summary", default="")
    pp.set_defaults(func=cmd_payload)
    pw = sub.add_parser("check-write", help="stdin=slot JSON; stdout=write|refuse:<other>")
    pw.add_argument("--goal", required=True)
    pw.set_defaults(func=cmd_check_write)
    pc = sub.add_parser("check-clear", help="stdin=slot JSON; stdout=clear|keep:<other>|absent")
    pc.add_argument("--goal", required=True)
    pc.set_defaults(func=cmd_check_clear)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001 - fail-open, see module docstring
        print(f"[close-owed-sentinel] WARN: {e}", file=sys.stderr)
        if args.cmd == "check-write":
            print("write", end="")
            return 0
        if args.cmd == "check-clear":
            print("absent", end="")
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
