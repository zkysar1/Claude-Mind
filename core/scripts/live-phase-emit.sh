#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Emit live_phase to team-state from execution-diary tail.
#
# Single source of truth: the diary tail. Partners use last_active for
# liveness — live_phase is strictly informational. A stale live_phase
# just means heartbeat-tick stopped running; check last_active age.
#
# CRITICAL INVARIANT for future devs: this script MUST NOT add internal
# fallbacks (try/except that synthesizes placeholder values, silent
# skips on missing fields, unrecognized-entry_type masking). The SINGLE
# fail-open boundary is heartbeat-tick.sh's `|| true` on the call site.
# Failures here must crash so stderr surfaces the problem; next tick
# retries naturally. Protecting at both layers is the cognitive-load
# trap the fresh-eyes review caught and removed.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

DIARY="$AGENT_DIR/session/execution-diary.jsonl"

if [ ! -s "$DIARY" ]; then
    LIVE_PHASE="no-diary"
else
    LIVE_PHASE=$(tail -1 "$DIARY" | python3 -c '
import sys, json
d = json.loads(sys.stdin.readline())
et = d["entry_type"]
phase = d.get("phase", "")
gid = d.get("goal_id", "")
if et == "phase_end":
    print("between-phases")
elif et == "phase_start":
    print(f"{phase} {gid}".strip())
elif et in ("finding", "decision", "failure", "approach_change",
            "observation", "state_update", "scorer_override"):
    # All remaining VALID_ENTRY_TYPES (execution-diary.py) — a legitimate
    # non-phase tail row must not kill the mirror. Truly unknown values
    # still crash below (the fresh-eyes no-masking invariant).
    #
    # SYNC OBLIGATION: this tuple + the two branches above must together
    # cover the VALID_ENTRY_TYPES set in execution-diary.py exactly. NOTE: no
    # apostrophes below — this whole block lives inside a bash single-quoted
    # string, so one possessive breaks the script at parse time (caught in
    # review here). It is a hand-kept
    # mirror (this script is IRREDUCIBLY LOCAL — importing the source of truth
    # would add the very indirection the header forbids), so adding an entry
    # type there without adding it here silently re-breaks this mirror.
    # scorer_override drifted exactly that way: added by  (Scorer
    # Sovereignty Layer B), never added here, so every heartbeat landing on a
    # deviation-claim tail row exited at the `else` below instead of writing
    # live_phase. Recognizing a DOCUMENTED type is not the masking the
    # invariant forbids — the `else` still crashes on genuinely unknown values.
    print(f"{et} {gid}".strip())
else:
    sys.exit(f"live-phase-emit: unhandled entry_type {et!r}")
')
fi

bash "$(dirname "$0")/team-state-update.sh" \
    --field "agent_status.$MIND_AGENT.live_phase" \
    --value "\"$LIVE_PHASE\""
