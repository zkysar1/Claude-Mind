#!/usr/bin/env bash
# Convention-style cached aspirations compact loader.
# Generates aspirations-compact.json from aspirations.jsonl if stale, then outputs
# the path only if not already tracked in context (like load-tree-summary.sh).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
ASP_JSONL="$WORLD_DIR/aspirations.jsonl"

# Cache lives in agent session dir (per-agent)
if [ -z "$AGENT_DIR" ]; then
    echo "[load-aspirations-compact] no agent bound, skipping" >&2
    exit 0
fi
COMPACT="$AGENT_DIR/session/aspirations-compact.json"

# Skip if aspirations.jsonl doesn't exist (fresh agent)
if [ ! -f "$ASP_JSONL" ]; then
    echo "[load-aspirations-compact] $ASP_JSONL not found, skipping" >&2
    exit 0
fi

# Regenerate if stale (aspirations.jsonl newer than cached compact)
if [ ! -f "$COMPACT" ] || [ "$ASP_JSONL" -nt "$COMPACT" ]; then
    python3 "$CORE_ROOT/scripts/aspirations.py" read --active-compact > "$COMPACT.tmp"
    mv "$COMPACT.tmp" "$COMPACT"
    # Content changed — clear stale tracker entry so agent re-Reads
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$COMPACT"
fi

# Output path only if not already tracked in context
python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$COMPACT"
