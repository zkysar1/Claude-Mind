#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-archive — daemon-aware wrapper (PR 9d).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source flag
#   3. POST /v1/aspirations/archive-sweep with source as query param
#   4. On 200, re-emit warnings[] to stderr and print archived_count to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
source "$CORE_ROOT/scripts/_argv_strict.sh"

# ONE literal, shared by the help text and the refusal message — never two
# copies (see argv_strict_refuse_unknown's header in _argv_strict.sh).
_ACCEPTED_FLAGS="--source <world|agent> | -h | --help"

SOURCE_VAL="world"
# () A write-only copy of the --source argv used to be kept here: nothing
# below ever read it, so it was deleted.

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        -h|--help)
            # BEFORE the -*) arm: --help is a `-*` token, and refusing it would be a
            # regression the guard introduced. Help exits 0 and sweeps NOTHING — the
            # old catch-all arm ignored it and ran the sweep ().
            argv_strict_help "$(basename "$0")" "[--source <world|agent>]" \
                "$_ACCEPTED_FLAGS" \
"  This command MUTATES: it SWEEPS completed/retired aspirations out of the live
  queue into the archive. There is no --dry-run (the daemon endpoint has none),
  so --help is the only probe that sweeps nothing. --source selects WHICH queue
  is swept and defaults to world."
            ;;
        -*)
            argv_strict_refuse_unknown "$(basename "$0")" "$1" "$_ACCEPTED_FLAGS";;
        *)
            argv_strict_refuse_extra_positional "$(basename "$0")" "$1" 0 "$_ACCEPTED_FLAGS";;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="source=${SOURCE_VAL}"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/archive-sweep \
    --query "$QUERY")" || rc=$?

case $rc in
    0)
        # 200: parse response. Re-emit warnings[] to stderr, print
        # archived_count to stdout (matches legacy CLI "print(str(count))" shape).
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print('WARNING: ' + w, file=sys.stderr)
print(resp.get('archived_count', 0))
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/archive-sweep \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print('WARNING: ' + w, file=sys.stderr)
print(resp.get('archived_count', 0))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-archive.sh";;
    *)
        exit $rc;;
esac
