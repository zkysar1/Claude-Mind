#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-14. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# aspirations-add-goal — daemon-aware wrapper (PR 7d).
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Parse args + read stdin body
#   3. POST /v1/aspirations/add-goal with --override-* mapped to headers
#   4. On 200, re-emit `warnings[]` to stderr and print `goal` to stdout
#      (matches legacy `json.dumps(goal, indent=2, ensure_ascii=False)`)
#
# --override-all maps to the X-Mind-Override-All header. The daemon
# add_goal endpoint owns the bulk fan-out (origin-signal / duplication /
# stale-read / no-investigate slots) and writes the blast-radius audit via
# _override_helpers.audit_bulk_override — single source of truth, no
# bash-side fan-out to split-brain with.
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Help (must precede main parse so it works even with no args) ---------
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    cat <<'USAGE'
Usage: aspirations-add-goal.sh [options] <asp_id>
       echo '<json-goal-body>' | aspirations-add-goal.sh [options] <asp_id>

Files a single goal under <asp_id>. The goal body comes from STDIN as JSON,
NOT as CLI flags.

Required:
  <asp_id>                     The aspiration to file under (e.g. asp-115).
                               Or pass via --aspiration <id>.
  <JSON body via stdin>        Goal object with at least: title, priority,
                               participants. Optional: description, category,
                               skill, origin_signal, etc.

Options:
  --help, -h                       Show this help.
  --aspiration <id>                Alias for positional <asp_id>.
  --source world|<agent>           Source aspirations file (default: world).
  --override-signal <reason>       Bypass origin-signal gate.
  --override-duplication <reason>  Bypass goal-duplication gate.
  --override-stale-read <reason>   Bypass stale-read gate.
  --override-no-investigate <reason>  Bypass no-investigate-slots gate.
  --override-all <reason>          Bulk-override all gates (audited).

Goal fields go in the JSON body — NOT as CLI flags. The following are
rejected as flags with an explicit error:
  --title, --description, --priority, --status, --participants,
  --category, --skill, --asp-id

Example:
  printf '%s' '{
    "title": "Idea: ...",
    "priority": "MEDIUM",
    "participants": ["agent"],
    "description": "...",
    "category": "framework-architecture"
  }' | bash core/scripts/aspirations-add-goal.sh asp-115

For full goal schema see core/config/conventions/aspirations.md.
USAGE
    exit 0
fi

# --- Parse args -----------------------------------------------------------
SOURCE_VAL="world"
ASP_ID=""
OVERRIDE_SIGNAL=""
OVERRIDE_DUPLICATION=""
OVERRIDE_STALE_READ=""
OVERRIDE_NO_INVESTIGATE=""
OVERRIDE_ALL=""
SCHEMA=0
declare -a PASSTHROUGH=()
declare -a PASSTHROUGH_SOURCE=()

# Value-arg pattern: "${2-}" + safe shift handle the no-value case under
# set -u (see _runtime.sh "Convention: value-arg parsing under set -u").
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source)
            SOURCE_VAL="${2-}"
            PASSTHROUGH_SOURCE=(--source "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --aspiration)
            # Named alias for the positional asp_id. Discoverable via --help;
            # common LLM-typed form. First --aspiration wins (matches positional
            # behavior below).
            [ -z "$ASP_ID" ] && ASP_ID="${2-}"
            shift $(( $# >= 2 ? 2 : 1 ));;
        --title|--description|--priority|--status|--participants|--category|--skill|--asp-id|--asp_id)
            # Field-shaped flags belong in the JSON body, not on the CLI.
            # Pre-cutover code silently passed these through to a now-removed
            # argparse fallback, producing an opaque "empty body" failure when
            # the LLM-typed --title pattern reached the daemon (alpha 2026-05-20).
            # Explicit reject + help pointer.
            echo "Error: '$1' is not a CLI flag for this script — goal fields go in the JSON body via stdin." >&2
            echo "Run: bash $0 --help" >&2
            exit 2;;
        --schema)
            SCHEMA=1
            PASSTHROUGH+=("$1"); shift;;
        --override-signal)
            OVERRIDE_SIGNAL="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-duplication)
            OVERRIDE_DUPLICATION="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-stale-read)
            OVERRIDE_STALE_READ="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-no-investigate)
            OVERRIDE_NO_INVESTIGATE="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        --override-all)
            OVERRIDE_ALL="${2-}"
            PASSTHROUGH+=("$1" "${2-}")
            shift $(( $# >= 2 ? 2 : 1 ));;
        -*)
            # Unknown flag — passthrough to daemon. The 2026-05-14 cutover
            # removed the Python CLI fallback, so unknown flags now just hit
            # the daemon (which rejects unknown query/header params silently).
            # If you suspect a typo, run --help.
            PASSTHROUGH+=("$1"); shift;;
        *)
            # Positional asp_id (first non-flag wins)
            [ -z "$ASP_ID" ] && ASP_ID="$1"
            PASSTHROUGH+=("$1"); shift;;
    esac
done

# --- Pre-daemon validation ------------------------------------------------
# --schema: print the accepted stdin-body field schema (machine-readable JSON
# form of the --help field list above) + a pointer to the authoritative
# convention doc. Keep the field list in sync with the --help block above and
# core/config/conventions/goal-schemas.md (the SSOT).
if [ "$SCHEMA" = "1" ]; then
    cat <<'SCHEMA_JSON'
{
  "writer": "aspirations-add-goal.sh",
  "input": "JSON goal object on stdin; <asp_id> as positional arg or --aspiration <id>",
  "required": {
    "title": "string - goal title (often prefixed Investigate:/Idea:/Apply:/Unblock:)",
    "priority": "string - HIGH | MEDIUM | LOW",
    "participants": "string[] - [agent], [agent,user], or [user]",
    "origin_signal": "string - upstream cause; prefixes: user_directive, board_post:<id>, pending_question:<id>, failing_test:<id>, resolved_hypothesis:<id>, low_confidence_node:<path>, recurring_cadence:<id>, investigate:<...>, idea:<...>, parent_aspiration:<id>"
  },
  "optional": {
    "description": "string",
    "category": "string",
    "skill": "string - e.g. /forge-skill; omit/null for inline code goals",
    "status": "string - pending (default) | in-progress | completed | blocked | skipped | expired",
    "recurring": "boolean",
    "interval_hours": "number - positive; required when recurring=true",
    "verification": "object - {outcomes: string[], checks: [{type,target,condition}], preconditions: [string | {type,id,...}]}",
    "handoff_to": "string - target agent for cross-agent handoff",
    "blocked_by": "string[] - goal-ids this goal is blocked on"
  },
  "auto_assigned": "id (format g-NNN-NN[N[N]]) and created - do NOT supply",
  "note": "Goal fields go in the JSON body via stdin, NOT as CLI flags.",
  "doc": "core/config/conventions/goal-schemas.md + core/config/conventions/aspirations.md"
}
SCHEMA_JSON
    exit 0
fi
if [ -z "$ASP_ID" ]; then
    echo "Error: asp_id is required." >&2
    exit 1
fi

# Read stdin (the JSON goal record) BEFORE invoking the daemon. The daemon
# expects the body as a request payload, not via stdin pipe.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

QUERY="asp_id=${ASP_ID}&source=${SOURCE_VAL}"

declare -a HEADER_ARGS=()
[ -n "$OVERRIDE_SIGNAL" ] && HEADER_ARGS+=(--header "X-Mind-Override-Signal: $OVERRIDE_SIGNAL")
[ -n "$OVERRIDE_DUPLICATION" ] && HEADER_ARGS+=(--header "X-Mind-Override-Duplication: $OVERRIDE_DUPLICATION")
[ -n "$OVERRIDE_STALE_READ" ] && HEADER_ARGS+=(--header "X-Mind-Override-Stale-Read: $OVERRIDE_STALE_READ")
[ -n "$OVERRIDE_NO_INVESTIGATE" ] && HEADER_ARGS+=(--header "X-Mind-Override-No-Investigate: $OVERRIDE_NO_INVESTIGATE")
[ -n "$OVERRIDE_ALL" ] && HEADER_ARGS+=(--header "X-Mind-Override-All: $OVERRIDE_ALL")

rc=0
RESPONSE="$(rt_call POST /v1/aspirations/add-goal \
    --query "$QUERY" \
    --body-string "$BODY" \
    "${HEADER_ARGS[@]+"${HEADER_ARGS[@]}"}")" || rc=$?

case $rc in
    0)
        # 200: parse response. Re-emit warnings[] to stderr and print the
        # full goal record to stdout (matches legacy CLI shape).
        # py launcher is OS-portable (rt_python_launcher). jq is not a
        # guaranteed dep, so we use python for the one-shot JSON parse.
        # shellcheck disable=SC2086
        printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    # Older daemon revision without 'goal' field — print the ack shape so
    # the wrapper still produces parseable JSON. Wrappers calling this
    # script must tolerate either shape during a rolling daemon upgrade.
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
        exit 0;;
    2)
        # Daemon answered 4xx/5xx; body already written to stderr by rt_curl.
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-14 cutover): no Python CLI fallback. Try one
        # auto-spawn, then fail loud. See .claude/rules/no-python-cli-fallback.md.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/aspirations/add-goal \
                --query "$QUERY" \
                --body-string "$BODY" \
                "${HEADER_ARGS[@]+"${HEADER_ARGS[@]}"}")" || rc=$?
            if [ "$rc" = "0" ]; then
                # shellcheck disable=SC2086
                printf '%s' "$RESPONSE" | $(rt_python_launcher) -c "
import json, sys
resp = json.load(sys.stdin)
for w in resp.get('warnings') or []:
    print(w, file=sys.stderr)
goal = resp.get('goal')
if goal is None:
    print(json.dumps({k: v for k, v in resp.items() if k != 'warnings'},
                     indent=2, ensure_ascii=False))
else:
    print(json.dumps(goal, indent=2, ensure_ascii=False))
"
                exit 0
            fi
        fi
        rt_no_daemon_error "aspirations-add-goal.sh";;
    *)
        exit $rc;;
esac
