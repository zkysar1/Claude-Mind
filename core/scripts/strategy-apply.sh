#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-29. No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
# strategy-apply — daemon-aware wrapper. Surface applicable meta-strategy
# heuristics during goal execution, or run a one-shot migrate backfill.
#
# Daemon paths:
#   POST /v1/meta/strategy-apply/match    (default — match heuristics)
#   POST /v1/meta/strategy-apply/migrate  (--migrate flag)
#
# Both endpoints return Response.text (byte-compat JSON), so the response
# body is emitted verbatim (no translation).
#
# Usage:
#   strategy-apply.sh --goal-category <cat> --goal-keywords "kw1,kw2,..." \
#                     [--phase selection|generation|any] [--increment]
#   strategy-apply.sh --migrate
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
GOAL_CATEGORY=""; GOAL_KEYWORDS=""; PHASE=""; INCREMENT=0; MIGRATE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --goal-category) GOAL_CATEGORY="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --goal-keywords) GOAL_KEYWORDS="${2-}"; shift $(( $# >= 2 ? 2 : 1 ));;
        --phase)         PHASE="${2-}";         shift $(( $# >= 2 ? 2 : 1 ));;
        --increment)     INCREMENT=1; shift;;
        --migrate)       MIGRATE=1; shift;;
        --json)          shift;;  # accepted for compat, no-op (CLI always outputs JSON)
        *) shift;;
    esac
done

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

if [ "$MIGRATE" = "1" ]; then
    # POST /v1/meta/strategy-apply/migrate — no body needed.
    ROUTE="/v1/meta/strategy-apply/migrate"

    rc=0
    rt_call POST "$ROUTE" --body-string "{}" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
            if rt_try_autospawn; then
                rc=0
                rt_call POST "$ROUTE" --body-string "{}" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "strategy-apply.sh";;
        *) exit $rc;;
    esac
else
    # POST /v1/meta/strategy-apply/match — JSON body.
    ROUTE="/v1/meta/strategy-apply/match"

    # Build JSON body. Use python for safe JSON encoding.
    BODY="$($(rt_python_launcher) -c "
import json, sys
body = {}
gc = sys.argv[1]
gk = sys.argv[2]
ph = sys.argv[3]
inc = sys.argv[4]
if gc: body['goal_category'] = gc
if gk: body['goal_keywords'] = gk
if ph: body['phase'] = ph
if inc == '1': body['increment'] = True
print(json.dumps(body))
" "$GOAL_CATEGORY" "$GOAL_KEYWORDS" "$PHASE" "$INCREMENT")"

    rc=0
    rt_call POST "$ROUTE" --body-string "$BODY" || rc=$?
    case $rc in
        0) exit 0;;
        2) exit 1;;
        3)
            # DAEMON-ONLY (2026-05-29 cutover): no Python CLI fallback.
            if rt_try_autospawn; then
                rc=0
                rt_call POST "$ROUTE" --body-string "$BODY" || rc=$?
                if [ "$rc" = "0" ]; then exit 0; fi
            fi
            rt_no_daemon_error "strategy-apply.sh";;
        *) exit $rc;;
    esac
fi
