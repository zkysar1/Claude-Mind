#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-add — daemon-aware wrapper (PR 49).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse --source and override flags
#   3. Read aspiration JSON from stdin
#   4. POST /v1/aspirations/add with source as query string
#   5. On 200, re-emit warnings[] to stderr, print aspiration to stdout
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SOURCE_VAL=""
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()
declare -a HEADERS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --schema)
            PASSTHROUGH+=("$1"); shift;;
        --override-signal)
            HEADERS+=(--header "X-Mind-Override-Signal: ${2-}")
            PASSTHROUGH+=("$1" "${2-}"); shift 2;;
        --override-duplication)
            HEADERS+=(--header "X-Mind-Override-Duplication: ${2-}")
            PASSTHROUGH+=("$1" "${2-}"); shift 2;;
        --override-all)
            # Send a single bulk header; the daemon fans this into any
            # unset per-gate slot (X-Mind-Override-Signal, X-Mind-Override-
            # Duplication) and audits the bulk override to
            # world/override-bypass-ledger.jsonl. Per-gate headers always win.
            HEADERS+=(--header "X-Mind-Override-All: ${2-}")
            PASSTHROUGH+=("$1" "${2-}"); shift 2;;
        *)
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# --- Pre-daemon validation ------------------------------------------------
# --schema: print the accepted stdin-body field schema (this writer adds an
# ASPIRATION, not a goal) + a pointer to the authoritative convention doc.
# Keep in sync with core/config/conventions/aspirations.md (the SSOT).
for arg in "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"; do
    if [ "$arg" = "--schema" ]; then
        cat <<'SCHEMA_JSON'
{
  "writer": "aspirations-add.sh",
  "input": "JSON aspiration object on stdin",
  "required": {
    "title": "string - aspiration title",
    "priority": "string - HIGH | MEDIUM | LOW"
  },
  "optional": {
    "status": "string - active (default) | completed | paused | retired",
    "scope": "string - sprint | project | initiative",
    "motivation": "string - why this aspiration exists",
    "description": "string",
    "tags": "string[]",
    "origin_signal": "string - upstream cause (see goal-schemas.md prefixes)",
    "goals": "array - goal objects; usually added separately via aspirations-add-goal.sh"
  },
  "auto_assigned": "id (format asp-NNN), progress, selection_count, archived - do NOT supply",
  "note": "Goals are normally filed under the aspiration afterward via aspirations-add-goal.sh.",
  "doc": "core/config/conventions/aspirations.md"
}
SCHEMA_JSON
        exit 0
    fi
done

# Read stdin BEFORE invoking the daemon (stdin is consumed once).
BODY="$(cat)"
if [ -z "$BODY" ]; then
    echo "Error: expected JSON on stdin" >&2
    exit 1
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY=""
[ -n "$SOURCE_VAL" ] && QUERY="source=${SOURCE_VAL}"

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/add \
    ${QUERY:+--query "$QUERY"} \
    --body-string "$BODY" \
    "${HEADERS[@]+"${HEADERS[@]}"}")" || rc=$?

case $rc in
    0)
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/add \
                ${QUERY:+--query "$QUERY"} \
                --body-string "$BODY" \
                "${HEADERS[@]+"${HEADERS[@]}"}")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
asp = resp.get('aspiration')
if asp is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(asp, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-add.sh";;
    *)
        exit $rc;;
esac
