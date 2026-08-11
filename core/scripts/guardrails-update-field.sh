#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 2). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# guardrails-update-field — daemon-aware wrapper. Updates a single field
# on a guardrails record.
#
# Usage:  guardrails-update-field.sh <id> <field> <value>
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
# STRICT. The previous `-*) shift;;` arm SWALLOWED any unknown flag, so
# `<id> <field> --value-file <path>` slid the PATH into VALUE and clobbered the
# record with rc=0 and no error. That destroyed guard-1615 (times_active 677,
# 1400+ chars -> an 87-char path string) on 2026-08-01; only a mandated
# read-back caught it. See .
#
# Two rules, both enforced BEFORE _runtime.sh is sourced so the refusal is cheap
# and cannot be masked by a downstream daemon failure:
#   1. an unknown leading-dash argument is an ERROR, never discarded
#   2. a 4th positional is an ERROR, never discarded
# Both exit 2 SPECIFICALLY (not merely non-zero) so a test can tell this refusal
# apart from a daemon/transport failure, which also exits non-zero.
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_argv_strict.sh"

argv_strict_parse "guardrails-update-field.sh" 3 "$@"
REC_ID="${ARGV_POS[0]:-}"
FIELD="${ARGV_POS[1]:-}"
VALUE="$(argv_strict_resolve_value "guardrails-update-field.sh" "${ARGV_POS[2]:-}")"

if [ -z "$REC_ID" ] || [ -z "$FIELD" ] || [ -z "$VALUE" ]; then
    argv_strict_usage "guardrails-update-field.sh" 3
    exit 1
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

QUERY="store=guardrails&id=$(rt_url_encode "$REC_ID")&field=$(rt_url_encode "$FIELD")&value=$(rt_url_encode "$VALUE")"

rc=0
RESPONSE="$(rt_call POST /v1/store/set-field \
    --query "$QUERY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2) exit 1;;
    3)
        # DAEMON-ONLY (2026-05-15 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/set-field \
                --query "$QUERY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
        fi
        rt_no_daemon_error "guardrails-update-field.sh";;
    *) exit $rc;;
esac
