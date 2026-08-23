#!/usr/bin/env bash
# DAEMON-ONLY. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Remove one `in_flight_bodies.<sid>` entry from an agent's team-state row, and
# sweep any null-valued siblings while the row lock is held.
# Daemon path: POST /v1/team-state/clear-body-row?agent=<name>&sid=<sid>
# Usage: bash core/scripts/team-state-clear-body-row.sh --agent <name> --sid <sid>
#
# WHY THIS EXISTS (). A body row used to be "cleared" by SETTING NULL,
# because the generic `team-state-update.sh --operation remove` is list-only and
# silently no-ops on a dict key while reporting ok:true — leaving one permanent
# null-valued key per SID an agent had ever run, on a shared synced store. This
# is the dedicated writer guard-2305 prescribes for a structured field.
#
# An already-absent --sid is a supported no-op: the null-sweep still runs, so
# this is also the drain path for residue left by the pre-fix behavior, whose
# SIDs belong to sessions that are gone.
#
# The arg loop ends in `*) shift;;`, so a flag without an explicit case is
# SILENTLY DISCARDED (guard-1776 class — see the sibling clear-in-flight
# wrapper, where exactly that swallowed --if-goal). Anyone adding a flag here
# must add its case.
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
AGENT=""
SID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --agent) AGENT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --sid)   SID="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --author) shift $(( $# >= 2 ? 2 : 1 ));;  # consumed by daemon via X-Mind-Agent header
        *) shift;;
    esac
done

if [ -z "$AGENT" ]; then
    echo "Error: --agent is required" >&2
    exit 1
fi
if [ -z "$SID" ]; then
    echo "Error: --sid is required" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="agent=$(rt_url_encode "$AGENT")&sid=$(rt_url_encode "$SID")"

_translate() {
    # Reproduce a CLI-shaped line from the daemon JSON response. Reads ONLY the
    # response on stdin — never re-opens WORLD_PATH to second-guess the daemon
    # (guard-582 / Gate 1b).
    # Daemon returns: {"ok","agent","sid","removed","nulls_swept","remaining"}.
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c '
import json, sys
resp = json.load(sys.stdin)
agent = resp.get("agent", "")
sid = resp.get("sid", "")
swept = resp.get("nulls_swept") or 0
left = resp.get("remaining") or 0
if resp.get("removed"):
    head = "body row removed for " + agent + "/" + sid
else:
    head = "body row already absent for " + agent + "/" + sid
print(head + " (nulls swept: " + str(swept) + ", remaining: " + str(left) + ")")
'
}

rc=0
RESPONSE="$(rt_call POST /v1/team-state/clear-body-row --query "$QUERY")" || rc=$?

case $rc in
    0) _translate "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY: no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/team-state/clear-body-row --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _translate "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "team-state-clear-body-row.sh";;
    *) exit $rc;;
esac
