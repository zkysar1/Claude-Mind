#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# Phase 4.25 bash-enforced goal-execution experience archive wrapper.
# Daemon path: rt_call POST /v1/experience/archive-goal.
#
# CLI args (--goal, --skill-slug, --category, --summary, --trace-file)
# plus optional stdin JSON are merged into a single JSON body.
# On success, prints the record JSON (indent=2, ensure_ascii=False) to
# stdout — byte-compat with the old CLI (experience.py archive-goal).
#
# Usage: [echo '{"tree_nodes_related":["x"]}' |] bash core/scripts/experience-archive-goal.sh \
#          --goal  --skill-slug aspirations-execute \
#          --category npc --summary "One-line summary" \
#          --trace-file agents/alpha/experience/trace.md
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse CLI args -------------------------------------------------------
GOAL=""; SKILL_SLUG=""; CATEGORY=""; SUMMARY=""; TRACE_FILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --goal)       GOAL="${2-}";       shift $(( $# >= 2 ? 2 : 1 ));;
        --skill-slug) SKILL_SLUG="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --category)   CATEGORY="${2-}";   shift $(( $# >= 2 ? 2 : 1 ));;
        --summary)    SUMMARY="${2-}";    shift $(( $# >= 2 ? 2 : 1 ));;
        --trace-file) TRACE_FILE="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        *) shift;;
    esac
done

# --- Read optional stdin JSON BEFORE sourcing _runtime.sh -----------------
STDIN_JSON=""
if [ ! -t 0 ]; then
    STDIN_JSON="$(cat)"
fi

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# Build JSON body: merge CLI args + optional stdin extra fields.
BODY="$($(rt_python_launcher) -c "
import json, sys
body = {}
stdin_json = sys.argv[1]
if stdin_json.strip():
    try:
        body = json.loads(stdin_json)
    except json.JSONDecodeError:
        pass
body['goal'] = sys.argv[2]
body['skill_slug'] = sys.argv[3]
body['category'] = sys.argv[4]
body['summary'] = sys.argv[5]
body['trace_file'] = sys.argv[6]
print(json.dumps(body, ensure_ascii=True))
" "$STDIN_JSON" "$GOAL" "$SKILL_SLUG" "$CATEGORY" "$SUMMARY" "$TRACE_FILE")"

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
RESPONSE="$(rt_call POST /v1/experience/archive-goal \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/experience/archive-goal \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "experience-archive-goal.sh";;
    *) exit $rc;;
esac
