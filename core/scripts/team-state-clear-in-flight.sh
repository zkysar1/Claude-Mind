#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Remove the in_flight block from an agent's status in the shared team state.
# Daemon path: POST /v1/team-state/clear-in-flight?agent=<name>[&if_goal=<id>]
# Usage: bash core/scripts/team-state-clear-in-flight.sh --agent <name> [--if-goal <goal-id>]
#
# --if-goal is an optional COMPARE-AND-SWAP (guard-2474 clause 2). With it, the
# row is cleared ONLY if its live goal_id matches, re-checked inside the row
# lock. Without it the clear is unconditional — byte-for-byte the pre-
# behavior, which retire/release/stop callers rely on to normalize whatever row
# is present. The endpoint and the CLI twin (team-state.py clear-in-flight)
# already accepted this parameter; this wrapper did not expose it until
# , and its arg loop ends in `*) shift;;` — so a `--if-goal` passed
# before that landed was SILENTLY DISCARDED and the clear ran unconditionally
# (guard-1776 class). Anyone adding a flag here must add an explicit case.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
AGENT=""
IF_GOAL=""
# SUPPLIED-ness is tracked separately from emptiness (). `-n "$IF_GOAL"`
# cannot tell `--if-goal ''` from no flag at all, and collapsing those two is the
# whole defect: a supplied-but-blank value would drop the query param, the daemon
# would read absent, and absent means CLEAR UNCONDITIONALLY — so a caller asking
# for a guard silently got a wipe. Same shape as the guard-1776 case in the header
# above, one layer in.
IF_GOAL_SET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent)   AGENT="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --if-goal) IF_GOAL="${2-}"; IF_GOAL_SET=1; shift $(( $# >= 2 ? 2 : 1 ));;
        --author) shift $(( $# >= 2 ? 2 : 1 ));;  # consumed by daemon via X-Mind-Agent header
        *) shift;;
    esac
done

if [ -z "$AGENT" ]; then
    echo "Error: --agent is required" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="agent=$(rt_url_encode "$AGENT")"
# Conditional-together (guard-527): the query param, the skipped-row branch in
# _translate, and the --if-goal flag all appear iff the caller asked for a CAS.
# With the flag absent the request is byte-identical to the pre- one.
#
# Gate on SUPPLIED-ness, not emptiness (): "the caller asked for a CAS"
# means the caller passed the flag. A blank value is now forwarded so the daemon
# can refuse it with a 400, instead of being dropped here and silently becoming
# an unconditional clear.
if [ "$IF_GOAL_SET" -eq 1 ]; then
    QUERY="$QUERY&if_goal=$(rt_url_encode "$IF_GOAL")"
fi

_translate() {
    # Reproduce CLI stdout from daemon JSON response. FOUR branches, mirroring
    # team-state.py cmd_clear_in_flight exactly (guard-742 dual-write): cleared /
    # left-alone / left-alone-unverifiable / already-absent. The third branch is
    # not cosmetic — without it a CAS that DECLINED to clear would print "already
    # absent", reporting a clear that never happened, which is the 
    # defect class (a verdict asserting a proven clear for a row that was left
    # standing). The FOURTH branch closes the same hole one layer down
    # (): skipped_goal_id is None on the unverifiable decline too, so
    # branch three cannot catch it and it fell through to "already absent".
    # Daemon returns: {"ok", "agent", "cleared", "skipped_goal_id", "row_survived"}.
    # IF_GOAL travels by ENV, never interpolated into the source (guard-165).
    # shellcheck disable=SC2086
    printf '%s' "$1" | IF_GOAL="$IF_GOAL" $(rt_python_launcher) -c '
import json, os, sys
resp = json.load(sys.stdin)
agent = resp.get("agent", "")
skipped = resp.get("skipped_goal_id")
if resp.get("cleared"):
    print("in_flight cleared for " + agent)
elif skipped:
    print("in_flight left alone for " + agent + ": row holds "
          + str(skipped) + ", not " + os.environ.get("IF_GOAL", ""))
elif resp.get("row_survived") is True:
    print("in_flight left alone for " + agent + ": row present but "
          "carries no comparable goal_id (not cleared)")
else:
    print("in_flight already absent for " + agent)
'
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/clear-in-flight --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/clear-in-flight --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-clear-in-flight.sh";;
    *) exit $rc;;
esac
