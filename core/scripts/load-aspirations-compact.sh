#!/usr/bin/env bash
# Convention-style cached aspirations compact loader.
# Generates aspirations-compact.json from aspirations.jsonl if stale, then outputs
# the path only if not already tracked in context (like load-tree-summary.sh).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
ASP_JSONL="$REPO_ROOT/mind/aspirations.jsonl"
COMPACT="$REPO_ROOT/mind/session/aspirations-compact.json"

# Skip if aspirations.jsonl doesn't exist (fresh agent)
[ -f "$ASP_JSONL" ] || exit 0

# Regenerate if stale (aspirations.jsonl newer than cached compact)
if [ ! -f "$COMPACT" ] || [ "$ASP_JSONL" -nt "$COMPACT" ]; then
    python3 "$CORE_ROOT/scripts/aspirations.py" read --active-compact > "$COMPACT.tmp"
    mv "$COMPACT.tmp" "$COMPACT"
    # Content changed — clear stale tracker entry so agent re-Reads
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$COMPACT"
fi

# Output path only if not already tracked in context
python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$COMPACT"
