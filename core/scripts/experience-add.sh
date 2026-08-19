#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# experience-add — daemon-aware wrapper. Appends an experience record
# from stdin JSON.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON experience record)
#   3. POST /v1/experience/add
#   4. On 200, print the record to stdout (indent=2, ensure_ascii=False)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SCHEMA=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --schema) SCHEMA=1; shift;;
        --help|-h)
            # A stdin-body reader HANGS on --help without this branch (guard-3145).
            echo "Usage: bash $0 < record.json   — the record is JSON on STDIN; there are NO field flags." >&2
            echo "Canonical form: core/config/conventions/stdin-json-inputs.md" >&2
            exit 0;;
        *)
            # Was `*) shift;;` — silently discarded flags, then blocked forever in
            # BODY="$(cat)" wherever stdin never delivers EOF ().
            echo "Error: '$1' is not a CLI flag for this script — the record goes in the JSON body via stdin." >&2
            echo "Run: bash $0 --help" >&2
            exit 2;;
    esac
done

if [ "$SCHEMA" = "1" ]; then
    echo "Error: --schema is no longer available. See mind_api/src/endpoints/experience_write.py for the experience record contract." >&2
    exit 1
fi

# Read stdin (the JSON experience record) BEFORE invoking the daemon.
BODY="$(cat)"

#  (G3 worker rail) — DEFENSIVE, sibling of the guard in
# journal-append.sh. A worker Body should never reach this writer: experience
# archival is a reducer-only phase per the work-partition contract. The guard
# converts a future phase-drift bug from silent shared-store corruption into a
# LOGGED skip; if it ever fires, that is a bug report about the partition.
#
# Placed AFTER stdin is consumed on purpose: exiting before `cat` would leave
# the caller writing into a closed pipe. Consume, then no-op.
#
# This script does a SKINNY PROJECT_ROOT resolve with no _paths.sh (see header),
# so agent_dir() is unavailable and the two path segments are inlined. They
# mirror AGENTS_PARENT_DIR and SESSIONS_DIRNAME and MUST be updated with them
# — CLAUDE.md "Agent-dir Resolution", inlined-copies table. Sourcing _paths.sh
# here would defeat the skinny-resolve contract this file is built on.
#
# MIND_SID is THIS process's own session, NOT running-session-id (the
# REDUCER's SID — the trap found at the health-ledger writer in this goal).
# Derived locally rather than from BODY_ROLE per guard-2445.
#
# Fail-open in every direction: unset vars or a missing file fall through to
# the normal write.
_APD="agents"
_SDN="sessions"
if [ -n "${MIND_SID:-}" ] && [ -n "${MIND_AGENT:-}" ] && \
   [ -f "$PROJECT_ROOT/$_APD/$MIND_AGENT/$_SDN/$MIND_SID/working-memory.yaml" ]; then
    echo "[experience-add] BODY=worker — SKIPPED agent-wide experience write. A worker should not reach this reducer-only writer; investigate the phase partition (g-306-125)." >&2
    exit 0
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_print_record() {
    # shellcheck disable=SC2086
    printf '%s' "$1" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
rec = resp.get('record') or resp
print(json.dumps(rec, indent=2, ensure_ascii=False))
"
}

rc=0
RESPONSE="$(rt_call POST /v1/experience/add \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/add \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-add.sh";;
    *) exit $rc;;
esac
