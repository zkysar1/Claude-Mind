#!/usr/bin/env bash
# Partner-presence reader — query world/presence/<agent>.jsonl for cross-agent visibility.
#  companion to presence-tick.sh.
#
# Usage:
#   bash core/scripts/presence-read.sh --agent <name> [--since <iso>] [--last <N>] [--json]
#
# --agent NAME    required: which agent's presence stream to read
# --since ISO     filter: only entries with ts >= ISO timestamp
# --last N        filter: only the last N entries (after --since filter)
# --json          output JSONL records as a JSON array (default: one record per line)
#
# Exit codes:
#   0 = ok (zero or more entries returned)
#   1 = invalid args / missing --agent
#   2 = presence file not found (no presence data for that agent yet)
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

AGENT=""
SINCE=""
LAST=""
JSON_OUT="0"

while [ $# -gt 0 ]; do
    case "$1" in
        --agent) AGENT="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --since) SINCE="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --last) LAST="$2"; shift $(( $# >= 2 ? 2 : 1 )) ;;
        --json) JSON_OUT="1"; shift ;;
        -h|--help)
            sed -n '2,15p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

if [ -z "$AGENT" ]; then
    echo "presence-read: --agent <name> required" >&2
    exit 1
fi

# --since is compared LEXICOGRAPHICALLY against the record's ISO `ts` (see the
# filter below), which is order-preserving for any ISO-8601 prefix and total
# nonsense for anything else. An unvalidated value therefore fails SILENTLY at
# rc=0, and in the direction that looks like an answer: measured on this box
# 2026-09-04 against alpha's 4081-record stream, `--since 30h` (the duration
# form board-read.sh accepts) returned 0 rows, as did `zzz` and `not-a-time`.
# Zero rows out of four thousand, reported as success. That is the 
# defect class one wrapper over — an unknown FLAG is refused loudly here
# (line 36) while an unparseable VALUE for a known flag was not ().
# Refuse it instead, naming the accepted form. Exit 1 per the contract above:
# a bad value is an invalid ARG, not a missing presence file (2).
if [ -n "$SINCE" ] && ! [[ "$SINCE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}([T\ ][0-9]{2}:[0-9]{2}(:[0-9]{2})?)?$ ]]; then
    echo "presence-read: --since '$SINCE' is not an ISO-8601 timestamp." >&2
    echo "  accepted: YYYY-MM-DD | YYYY-MM-DDTHH:MM | YYYY-MM-DDTHH:MM:SS" >&2
    echo "  note: a DURATION such as '30h' is NOT accepted here (board-read.sh takes those; this reader does not)." >&2
    exit 1
fi

PRESENCE_FILE="$WORLD_DIR/presence/$AGENT.jsonl"
if [ ! -f "$PRESENCE_FILE" ]; then
    echo "presence-read: no presence file at $PRESENCE_FILE" >&2
    exit 2
fi

python3 - "$PRESENCE_FILE" "$SINCE" "$LAST" "$JSON_OUT" <<'PYEOF'
import json, sys
path, since, last, json_out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
records = []
with open(path, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since and r.get("ts", "") < since:
            continue
        records.append(r)
if last:
    try:
        records = records[-int(last):]
    except ValueError:
        pass
if json_out == "1":
    print(json.dumps(records, ensure_ascii=False))
else:
    for r in records:
        print(json.dumps(r, ensure_ascii=False))
PYEOF
