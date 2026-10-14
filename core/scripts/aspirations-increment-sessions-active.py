#!/usr/bin/env python3
"""aspirations-increment-sessions-active.py — atomic per-aspiration sessions_active
counter increment. Implements the Phase 8.1 promise from
core/config/aspirations-loop-digest.md:154 ("asp.sessions_active += 1") that was
previously LLM-residue pseudocode never bashified.

Filed by g-001-217 (HIGH Unblock) after g-001-216 investigation found:
- All 17 active aspirations had sessions_active=0 (counter never advances)
- Read in 3 places (aspirations.py:2031 maturity gate, precheck-eval.py:399
  stalled detection, aspirations-complete-review:346 Phase 7.6 deepening)
- Written nowhere
- Maturity gate would falsely deepen near-complete aspirations on closure
- Stalled-detection branch was effectively dead code

Caller: iteration-close.sh do_state_update (per-goal-close), gated by a
session-scoped tracking file so first-time-per-session semantics match the
LLM-side loop_state.touched but bash-owned for SSOT consistency with the
rb-686 / rb-428 LLM-residue → bash promotion pattern.

Inputs (env vars):
    ASP_ID:  aspiration ID to increment (e.g., asp-115)
    SOURCE:  "world" or "agent" (selects which JSONL to update)

Output:
    stdout: "sessions_active incremented for <id> -> <new_value>" on success
    stderr: warning on failure
    exit 0 always (fail-open — never block iteration-close)

Write path: _fileops.locked_modify_jsonl, the primitive every other update path
uses (lock, refresh, canonical json.dumps(ensure_ascii=True), fenced backend PUT).
g-358-202: this script used to re-read the local file and os.replace it with
json.dumps(ensure_ascii=False) records. The composite writer's split() refuses
such a body (its round trip is the ensure_ascii=True form), so the sync push of
that file went out as one whole 32 MB object, the daily "flip", flag or no flag.
Every flip since 2026-10-10 differs from the version before it by exactly one
sessions_active += 1.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import WORLD_DIR, AGENT_DIR  # type: ignore
from _fileops import locked_modify_jsonl  # type: ignore


def main():
    asp_id = os.environ.get("ASP_ID", "").strip()
    src = os.environ.get("SOURCE", "world").strip()

    if not asp_id:
        print("aspirations-increment-sessions-active: ASP_ID empty", file=sys.stderr)
        sys.exit(0)

    target = Path(AGENT_DIR if src == "agent" else WORLD_DIR) / "aspirations.jsonl"
    if not target.exists():
        print(
            f"aspirations-increment-sessions-active: {target} missing",
            file=sys.stderr,
        )
        sys.exit(0)

    new_val = []

    def _bump(items):
        new_val.clear()  # the modifier re-runs on a fenced-write retry
        for asp in items:
            if asp.get("id") == asp_id:
                asp["sessions_active"] = int(asp.get("sessions_active", 0)) + 1
                new_val.append(asp["sessions_active"])
        return items

    try:
        locked_modify_jsonl(target, _bump)
        if new_val:
            print(f"sessions_active incremented for {asp_id} -> {new_val[-1]}")
        else:
            print(
                f"aspirations-increment-sessions-active: {asp_id} not found in {target}",
                file=sys.stderr,
            )
    except Exception as e:
        print(
            f"aspirations-increment-sessions-active: error for {asp_id}: {e}",
            file=sys.stderr,
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
