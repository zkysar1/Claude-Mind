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
COMPACT_SUMMARY="$AGENT_DIR/session/aspirations-compact-summary.json"
AGENT_JSONL="$AGENT_DIR/aspirations.jsonl"

# Skip if neither aspirations file exists (fresh setup)
if [ ! -f "$WORLD_JSONL" ] && [ ! -f "$AGENT_JSONL" ]; then
    echo "[load-aspirations-compact] no aspirations files found, skipping" >&2
    exit 0
fi

# Regenerate if stale (either source newer than cached compact)
STALE=0
if [ ! -f "$COMPACT" ] || [ ! -f "$COMPACT_SUMMARY" ]; then STALE=1
elif [ -f "$WORLD_JSONL" ] && [ "$WORLD_JSONL" -nt "$COMPACT" ]; then STALE=1
elif [ -f "$AGENT_JSONL" ] && [ "$AGENT_JSONL" -nt "$COMPACT" ]; then STALE=1
fi

if [ "$STALE" = "1" ]; then
    # Both queues loaded and merged — goal-selector reads from both,
    # so compact data must include both for dedup and iteration.
    # DAEMON-ONLY (2026-05-14 cutover): aspirations.py `read` was deleted;
    # route through the daemon-aware wrapper. See
    # .claude/rules/no-python-cli-fallback.md
    bash "$CORE_ROOT/scripts/aspirations-read.sh" --active-compact > "$COMPACT.tmp.w"
    bash "$CORE_ROOT/scripts/aspirations-read.sh" --source agent --active-compact > "$COMPACT.tmp.a"
    # Two output files, atomic rename:
    #   COMPACT          — full canonical compact for Python consumers
    #                      (precheck-eval, findings-gate, etc. read directly).
    #                      No token cap; can grow large.
    #   COMPACT_SUMMARY  — trimmed projection for LLM Read-tool consumption
    #                      (~25k-token cap). Drops completed-non-recurring
    #                      goals and prunes verbose fields.  (bravo
    #                      session 59 iter-18): full compact grew to 297KB
    #                      organically as 972 completed-non-recurring goals
    #                      accumulated across active aspirations; LLM Read of
    #                      the full file failed at 123K tokens. Summary is
    #                      ~57KB / ~24K tokens, under cap.
    # indent=None + tight separators preserved (: indent=2 inflated
    # 234KB → 335KB purely via whitespace). SSOT for both files.
    python3 -c "
import json, sys
SUMMARY_KEEP = {'id','title','status','priority','category','skill','recurring','interval_hours','lastAchievedAt','participants','blocked_by','deferred_until','defer_reason','depends_on','started'}
w = json.load(open(sys.argv[1]))
a = json.load(open(sys.argv[2]))
merged = w + a
# Full canonical compact
with open(sys.argv[3], 'w', encoding='utf-8') as fh:
    json.dump(merged, fh, indent=None, separators=(',', ':'), ensure_ascii=True)
# LLM-facing summary projection
summary = []
for asp in merged:
    new_asp = {k: v for k, v in asp.items() if k != 'goals'}
    new_asp['goals'] = [
        {k: v for k, v in g.items() if k in SUMMARY_KEEP}
        for g in asp.get('goals', [])
        if not (g.get('status') == 'completed' and not g.get('recurring'))
    ]
    summary.append(new_asp)
with open(sys.argv[4], 'w', encoding='utf-8') as fh:
    json.dump(summary, fh, indent=None, separators=(',', ':'), ensure_ascii=True)
" "$COMPACT.tmp.w" "$COMPACT.tmp.a" "$COMPACT.tmp" "$COMPACT_SUMMARY.tmp"
    mv "$COMPACT.tmp" "$COMPACT"
    mv "$COMPACT_SUMMARY.tmp" "$COMPACT_SUMMARY"
    rm -f "$COMPACT.tmp.w" "$COMPACT.tmp.a"
    # Content changed — clear stale tracker entries so agent re-Reads
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$COMPACT"
    python3 "$CORE_ROOT/scripts/context-reads.py" invalidate "$COMPACT_SUMMARY"
fi

# Output the LLM-facing SUMMARY path (orchestrator's `IF path returned: Read it`
# now consumes the trimmed projection). Python consumers reference the full
# COMPACT path directly via AGENT_DIR / "session" / "aspirations-compact.json".
python3 "$CORE_ROOT/scripts/context-reads.py" check-file "$COMPACT_SUMMARY"
