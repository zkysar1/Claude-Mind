#!/usr/bin/env bash
# Convention-style cached aspirations compact loader.
# Generates aspirations-compact.json from aspirations.jsonl if stale, then outputs
# the path only if not already tracked in context (like load-tree-summary.sh).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
WORLD_JSONL="$WORLD_DIR/aspirations.jsonl"

# Cache lives in agent session dir (per-agent)
if [ -z "$AGENT_DIR" ]; then
    echo "[load-aspirations-compact] no agent bound, skipping" >&2
    exit 0
fi
COMPACT="$AGENT_DIR/session/aspirations-compact.json"
AGENT_JSONL="$AGENT_DIR/aspirations.jsonl"

# Skip if neither aspirations file exists (fresh setup)
if [ ! -f "$WORLD_JSONL" ] && [ ! -f "$AGENT_JSONL" ]; then
    echo "[load-aspirations-compact] no aspirations files found, skipping" >&2
    exit 0
fi

# Regenerate if stale (either source newer than cached compact)
STALE=0
if [ ! -f "$COMPACT" ]; then STALE=1
elif [ -f "$WORLD_JSONL" ] && [ "$WORLD_JSONL" -nt "$COMPACT" ]; then STALE=1
elif [ -f "$AGENT_JSONL" ] && [ "$AGENT_JSONL" -nt "$COMPACT" ]; then STALE=1
fi

if [ "$STALE" = "1" ]; then
    # Both queues loaded and merged — goal-selector reads from both,
    # so compact data must include both for dedup and iteration.
    python3 "$CORE_ROOT/scripts/aspirations.py" read --active-compact > "$COMPACT.tmp.w"
    python3 "$CORE_ROOT/scripts/aspirations.py" --source agent read --active-compact > "$COMPACT.tmp.a"
    # Merge (source field injected by aspirations.py compact_aspiration)
    python3 -c "
import json, sys
w = json.load(open(sys.argv[1])); a = json.load(open(sys.argv[2]))
json.dump(w + a, sys.stdout, indent=2, ensure_ascii=True)
" "$COMPACT.tmp.w" "$COMPACT.tmp.a" > "$COMPACT.tmp"
    mv "$COMPACT.tmp" "$COMPACT"
    rm -f "$COMPACT.tmp.w" "$COMPACT.tmp.a"
    # Content changed — clear stale tracker entry so agent re-Reads
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$COMPACT"
fi

# Output path only if not already tracked in context
python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$COMPACT"
