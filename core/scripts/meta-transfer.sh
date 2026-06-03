#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# meta-transfer.sh — Export/import meta-strategy bundles for cross-domain transfer.
#
# Daemon paths:
#   POST /v1/meta/transfer-export  {output}
#   POST /v1/meta/transfer-import  {input, dry_run}
#
# Both endpoints return Response.text with byte-compat-to-CLI JSON,
# so the response body is emitted verbatim (no translation).
#
# Usage:
#   meta-transfer.sh export --output <path>
#   meta-transfer.sh import --input <path> [--dry-run]
set -euo pipefail

_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse subcommand + args ------------------------------------------------
CMD="${1-}"
if [ -z "$CMD" ]; then
    echo "Usage: meta-transfer.sh {export|import} [options]" >&2
    exit 1
fi
shift

OUTPUT=""
INPUT=""
DRY_RUN=0

case "$CMD" in
    export)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --output) OUTPUT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                *) shift;;
            esac
        done
        ;;
    import)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --input)   INPUT="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
                --dry-run) DRY_RUN=1; shift;;
                *) shift;;
            esac
        done
        ;;
    *)
        echo "Unknown subcommand: $CMD" >&2
        echo "Usage: meta-transfer.sh {export|import} [options]" >&2
        exit 1
        ;;
esac

# --- Daemon path ------------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

_do_call() {
    case "$CMD" in
        export)
            if [ -z "$OUTPUT" ]; then
                echo "Error: --output is required for export" >&2
                return 1
            fi
            local BODY
            BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({'output': sys.argv[1]}))
" "$OUTPUT")
            rt_call POST /v1/meta/transfer-export --body-string "$BODY"
            ;;
        import)
            if [ -z "$INPUT" ]; then
                echo "Error: --input is required for import" >&2
                return 1
            fi
            local BODY
            if [ "$DRY_RUN" = "1" ]; then
                BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({'input': sys.argv[1], 'dry_run': True}))
" "$INPUT")
            else
                BODY=$($(rt_python_launcher) -c "
import json, sys
print(json.dumps({'input': sys.argv[1]}))
" "$INPUT")
            fi
            rt_call POST /v1/meta/transfer-import --body-string "$BODY"
            ;;
    esac
}

rc=0
_do_call || rc=$?

case $rc in
    0) exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            _do_call || rc=$?
            if [ "$rc" = "0" ]; then exit 0; fi
        fi
        rt_no_daemon_error "meta-transfer.sh";;
    *) exit $rc;;
esac
