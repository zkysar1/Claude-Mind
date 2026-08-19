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
    # Write to temp first — if tree-read.sh fails, don't corrupt the cached file.
    # The generator's payload is piped through _tree_summary.py, which bounds it
    # to a FRACTION OF THE READ-TOOL CAP (). Unbounded, this file grew
    # with the tree and reached 3.63x the cap over 1,372 nodes, so the callers'
    # "IF path returned: Read it" contract could not complete at all.
    #
    # The BOUND LIVES HERE AND NOT IN tree-read.sh DELIBERATELY: that command has
    # a second consumer (aspirations-strategic-scan reads its stdout directly) at
    # full fidelity, and narrowing the generator would silently narrow that reader
    # too. Only this cached LLM-facing projection is bounded.
    #
    # pipefail (set at the top) is load-bearing: if either stage fails, the temp
    # file is never moved and the previous cached summary survives intact.
    bash "$CORE_ROOT/scripts/tree-read.sh" --summary \
        | python3 "$CORE_ROOT/scripts/_tree_summary.py" > "$TMP_SUMMARY"
    mv "$TMP_SUMMARY" "$SUMMARY_JSON"
    # Content changed — clear stale tracker entry so agent re-Reads
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$SUMMARY_JSON"
fi

# Output path only if not already tracked in context
python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$SUMMARY_JSON"
