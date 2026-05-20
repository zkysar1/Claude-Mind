#!/usr/bin/env bash
# Read a session binding by SID. Thin shell wrapper around _session_binding.py.
#
# Usage:
#   bash core/scripts/session-binding-read.sh <SID> [--field <field>] [--json]
#
# Fields: agent | mode | session_id | started_at | started_by | source
# Default (no --field): prints the agent name (legacy-compatible output).
# --json: prints the full record as JSON.
#
# Exit codes:
#   0  resolved successfully (output on stdout)
#   1  unresolvable (no output)
#   2  bad usage
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$#" -lt 1 ]; then
    echo "usage: session-binding-read.sh <SID> [--field <field>] [--json]" >&2
    exit 2
fi

SID="$1"
shift

FIELD=""
EMIT_JSON=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --field)
            FIELD="$2"
            shift 2
            ;;
        --json)
            EMIT_JSON=1
            shift
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

OUT=$(py -3 "$SCRIPT_DIR/_session_binding.py" "$SID" 2>/dev/null || true)
if [ -z "$OUT" ]; then
    exit 1
fi

if [ "$EMIT_JSON" -eq 1 ]; then
    printf '%s\n' "$OUT"
    exit 0
fi

# Field selector. Use py -3 to extract from the JSON line for robustness.
if [ -n "$FIELD" ]; then
    py -3 -c "
import json, sys
data = json.loads('''$OUT''')
v = data.get('$FIELD')
if v is None or data.get('resolved') is False:
    sys.exit(1)
print(v)
"
    exit $?
fi

# Default: print agent name (legacy-compatible).
py -3 -c "
import json, sys
data = json.loads('''$OUT''')
if data.get('resolved') is False:
    sys.exit(1)
print(data.get('agent', ''))
"
