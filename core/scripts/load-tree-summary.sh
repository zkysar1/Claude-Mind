#!/usr/bin/env bash
# Convention-style cached tree summary loader.
# Generates _summary.json from _tree.yaml if stale, then outputs
# the path only if not already tracked in context (like load-conventions.sh).
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
TREE_YAML="$WORLD_DIR/knowledge/tree/_tree.yaml"
SUMMARY_JSON="$WORLD_DIR/knowledge/tree/_summary.json"
TMP_SUMMARY="$SUMMARY_JSON.tmp"

# Regenerate if stale (tree.yaml newer than cached summary)
if [ ! -f "$SUMMARY_JSON" ] || [ "$TREE_YAML" -nt "$SUMMARY_JSON" ]; then
    # Write to temp first — if tree-read.sh fails, don't corrupt the cached file
    bash "$CORE_ROOT/scripts/tree-read.sh" --summary > "$TMP_SUMMARY"
    mv "$TMP_SUMMARY" "$SUMMARY_JSON"
    # Content changed — clear stale tracker entry so agent re-Reads
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$SUMMARY_JSON"
fi

# Output path only if not already tracked in context
python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$SUMMARY_JSON"
