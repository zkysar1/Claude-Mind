#!/usr/bin/env python3
"""close-defer-invalidation.py — advisory close-time dependent-defer recheck (2).

rb-3946 incident: a goal close declared a premise unblocked, but open goals
whose defer_reason rested on that premise stayed deferred until TTL lapse
(~11h frozen). The existing sweeps do not cover premise-level invalidation:
defer-recheck.sh auto-clears only defers naming a COMPLETED goal id in
g-NNN-NN form; unblock-parent-status / parent-supersession key on title
shapes. This script closes the observability half of that gap at close time.

Called from iteration-close.sh do_verify (status=completed path) with the
just-closed goal. It scans open (pending/in-progress) goals across both
queues for defer_reason text that references the closed goal — by exact id
OR by >=2 significant title-token overlaps — and prints one ADVISORY line
per hit so the LLM re-probes that defer premise-by-premise.

ADVISORY ONLY: never mutates goal state, never files goals (surface, not
auto-clear — the defer premise may reference the closed goal yet still hold).
Fail-open: every error path prints a stderr WARN and exits 0; a missed
advisory self-heals via the defer TTL, so this must never break a close.
"""

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import _rt  # canonical Python -> daemon client

# Tokens too generic to indicate a premise link on their own.
_STOPWORDS = {
    "about", "after", "again", "agent", "before", "check", "close", "fix",
    "goal", "goals", "recurring", "restore", "should", "sweep", "their",
    "these", "update", "verify", "world", "which", "while", "would",
}


def _read_open_goals(source):
    try:
        out = _rt.aspirations_read(source=source, active=True)
    except _rt.RtError as e:
        print(f"[close-defer-invalidation] WARN: {source} read failed: {e.body or e}",
              file=sys.stderr)
        return []
    try:
        data = _rt.tolerant_decode_aggregate(f"close-defer-invalidation: {source}", out)
    except SystemExit:
        return []
    except Exception as e:
        print(f"[close-defer-invalidation] WARN: {source} decode failed: {e}",
              file=sys.stderr)
        return []
    goals = []
    for asp in (data.get("aspirations") if isinstance(data, dict) else data) or []:
        for g in asp.get("goals", []) or []:
            if g.get("status") in ("pending", "in-progress") and g.get("defer_reason"):
                g["_source"] = source
                goals.append(g)
    return goals


def _significant_tokens(text):
    return {
        t for t in re.findall(r"[a-z0-9][a-z0-9_-]{4,}", (text or "").lower())
        if t not in _STOPWORDS
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", required=True, help="just-closed goal id")
    ap.add_argument("--title", default="", help="just-closed goal title (token matching)")
    args = ap.parse_args()

    closed_id = args.goal.strip()
    title_tokens = _significant_tokens(args.title)

    open_deferred = _read_open_goals("world") + _read_open_goals("agent")
    if not open_deferred:
        return 0

    for g in open_deferred:
        reason = str(g.get("defer_reason") or "")
        matched = None
        # Word-boundary id match: a bare substring check false-flags when the
        # closed id is a PREFIX of a longer id in the defer text (
        # inside 2) — confirmed by the fresh-eyes probe 2026-07-18.
        if closed_id and re.search(re.escape(closed_id) + r"(?![0-9-])", reason):
            matched = f"names closed id {closed_id}"
        elif title_tokens:
            overlap = title_tokens & _significant_tokens(reason)
            if len(overlap) >= 2:
                matched = "title-token overlap: " + ", ".join(sorted(overlap)[:4])
        if matched:
            print(
                f"▸ DEFER-INVALIDATION ADVISORY: close of {closed_id} may invalidate "
                f"the defer on {g.get('id')} ({g['_source']}) — {matched}. "
                f"Re-probe that defer premise-by-premise NOW instead of waiting for "
                f"TTL lapse (rb-3946). defer_reason: {reason[:140]}"
            )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # advisory: never break the close
        print(f"[close-defer-invalidation] WARN: {e}", file=sys.stderr)
        sys.exit(0)
