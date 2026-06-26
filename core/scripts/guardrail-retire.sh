#!/usr/bin/env bash
# guardrail-retire.sh - D1 guardrail cluster retirement wrapper ().
#
# Thin companion over guardrail_retire.py (the engine). `scan`/`cluster` forward
# to the engine (read-only); `apply`/`restore` execute the engine's MUTATION PLAN
# via guardrails-update-field.sh. world/guardrails.jsonl is own-cloud append-JSONL
# (guard-832) - the engine NEVER writes it directly; the status flip + stamp go
# through the daemon wrapper. The engine is invoked as a TERMINAL python process
# (py -3 <file>); the plan is parsed by a TERMINAL `py -3 -c`; the mutations are
# executed by a BASH loop calling guardrails-update-field.sh - so there is no
# Python->bash->python hop (rb-225/rb-247 hang is avoided).
#
# Usage:
#   guardrail-retire.sh scan [--scope-category C] [--today YYYY-MM-DD]
#   guardrail-retire.sh cluster <guard-id>
#   guardrail-retire.sh apply <guard-id> <keep|refresh|retire|revise> [--force] [--reason R] [--today D]
#   guardrail-retire.sh restore <guard-id>
set -euo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF/../.." && pwd)"
export PROJECT_ROOT
ENGINE="$PROJECT_ROOT/core/scripts/guardrail_retire.py"
UPD="$PROJECT_ROOT/core/scripts/guardrails-update-field.sh"

# Source _paths.sh for env (the engine resolves WORLD_DIR via `from _paths import
# WORLD_DIR`; sourcing keeps the python shim on PATH per python-invocation.md).
# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh" 2>/dev/null || true

# Portable terminal-python runner: py -3 (Windows launcher, reliable) then
# python3 then python. Never `-c`-spawns bash, so no rb-225/rb-247 hop.
if command -v py >/dev/null 2>&1; then _PY="py -3"
elif command -v python3 >/dev/null 2>&1; then _PY="python3"
else _PY="python"; fi

CMD="${1:-}"
[ -n "$CMD" ] || { echo "Usage: guardrail-retire.sh {scan|cluster|apply|restore} ..." >&2; exit 1; }
shift || true

case "$CMD" in
    scan|cluster)
        $_PY "$ENGINE" "$CMD" "$@"
        ;;
    apply|restore)
        # 1. Compute the mutation plan (read-only).
        rc=0
        PLAN="$($_PY "$ENGINE" "$CMD" "$@")" || rc=$?
        echo "$PLAN"
        if [ "$rc" != "0" ]; then exit "$rc"; fi
        # 2. Execute the plan's mutations in BASH (py parses, bash executes the
        #    daemon wrapper). Tab-separated id<TAB>field<TAB>value lines.
        MUTS="$(printf '%s' "$PLAN" | $_PY -c "
import json, sys
plan = json.load(sys.stdin)
if not plan.get('ok'):
    sys.exit(0)
for m in plan.get('mutations', []):
    print('%s\t%s\t%s' % (m['id'], m['field'], m['value']))
")"
        exec_rc=0
        if [ -n "$MUTS" ]; then
            while IFS=$'\t' read -r mid mfield mvalue; do
                [ -n "$mid" ] || continue
                if bash "$UPD" "$mid" "$mfield" "$mvalue" >/dev/null 2>&1; then
                    echo "  mutate ${mid}.${mfield}=${mvalue} -> ok"
                else
                    echo "  mutate ${mid}.${mfield}=${mvalue} -> FAILED" >&2
                    exec_rc=1
                fi
            done <<< "$MUTS"
        fi
        exit "$exec_rc"
        ;;
    *)
        echo "Unknown command: $CMD (expected scan|cluster|apply|restore)" >&2
        exit 1
        ;;
esac
