#!/usr/bin/env bash
# DAEMON-ONLY as of 2026-05-15 (H2 Wave 2). No Python CLI fallback. See:
#   .claude/rules/no-python-cli-fallback.md
#   world/knowledge/tree/system/daemon-only-architecture.md
#   zeta/reports/phase3-h2-wave-plan.md (generic store endpoint)
# reasoning-bank-add — daemon-aware wrapper. Appends a reasoning-bank
# record from stdin JSON.
#
# Hot path:
#   1. Skinny PROJECT_ROOT resolve (no _paths.sh)
#   2. Read stdin body (JSON reasoning-bank record)
#   3. POST /v1/store/append?store=reasoning-bank
#   4. On 200, print the record to stdout (indent=2, ensure_ascii=False)
#
set -euo pipefail

# --- Skinny PROJECT_ROOT resolve ------------------------------------------
_RUNTIME_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_RUNTIME_SELF/../.." && pwd)"
CORE_ROOT="$PROJECT_ROOT/core"

# --- Parse args -----------------------------------------------------------
SCHEMA=0
ALLOW_NEAR_DUP=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --schema) SCHEMA=1; shift;;
        --allow-near-dup) ALLOW_NEAR_DUP=1; shift;;
        --help|-h)
            # A stdin-body reader HANGS on --help without this branch (guard-3145).
            echo "Usage: bash $0 < record.json   — the record is JSON on STDIN; field values NEVER come from flags. --allow-near-dup bypasses the near-duplicate refusal (g-115-6948)." >&2
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
    echo "Error: --schema is no longer available. See mind_api/src/endpoints/store.py + mind_api/src/store_registry.py for the reasoning-bank record contract." >&2
    exit 1
fi

# Read stdin (the JSON reasoning-bank record) BEFORE invoking the daemon.
BODY="$(cat)"

# --- Daemon path ----------------------------------------------------------
# shellcheck disable=SC1091
source "$CORE_ROOT/scripts/_runtime.sh"

# : forward the near-dup override as a transport field the daemon
# pops before validation. Injected here (not by the caller) so the flag stays
# a CLI concern and the JSON contract on stdin is unchanged.
if [ "${ALLOW_NEAR_DUP}" = "1" ]; then
    # shellcheck disable=SC2086
    BODY="$(printf '%s' "$BODY" | $(rt_python_launcher) -c "
import json, sys
d = json.load(sys.stdin)
d['allow_near_dup'] = True
print(json.dumps(d, ensure_ascii=False))
")"
fi

# --- Advisory near-duplicate warning () --------------------------
# Prints ONE stderr advisory when this record closely resembles an existing
# ACTIVE entry. ADVISORY ONLY — it MUST NOT gate the add: a false positive that
# refuses a legitimate entry is worse than the duplicate it would have prevented.
# The helper always exits 0 on its own; `|| true` is the second guard so a
# helper crash cannot trip `set -e` and fail the append. Runs BEFORE the append
# so the warning can name the EXISTING entry — the new record's id is assigned
# server-side inside the lock and is not known here.
# shellcheck disable=SC2086
printf '%s' "$BODY" | $(rt_python_launcher) "$CORE_ROOT/scripts/store_dupe_warn.py" \
    --store reasoning-bank || true

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
RESPONSE="$(rt_call POST /v1/store/append \
    --query "store=reasoning-bank" \
    --body-string "$BODY")" || rc=$?

case $rc in
    0) _print_record "$RESPONSE"; exit 0;;
    2)
        # Surface the daemon's refusal verbatim before the nonzero exit
        # (guard-1673: never discard the consumer's precise reason; guard-1720:
        # a rejected write must not read as success). A 409 near_duplicate
        # () carries the strengthen-instead pointer the writer needs.
        if [ -n "${RESPONSE:-}" ]; then printf '%s\n' "$RESPONSE" >&2; fi
        exit 1;;
    3)
        # DAEMON-ONLY (2026-05-15 cutover): no Python CLI fallback.
        if rt_try_autospawn; then
            rc=0
            RESPONSE="$(rt_call POST /v1/store/append \
                --query "store=reasoning-bank" \
                --body-string "$BODY")" || rc=$?
            if [ "$rc" = "0" ]; then _print_record "$RESPONSE"; exit 0; fi
            if [ "$rc" = "2" ]; then
                if [ -n "${RESPONSE:-}" ]; then printf '%s\n' "$RESPONSE" >&2; fi
                exit 1
            fi
        fi
        rt_no_daemon_error "reasoning-bank-add.sh";;
    *) exit $rc;;
esac
