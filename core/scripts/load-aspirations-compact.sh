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
    # Fallback (, zeta-1094): hook may have injected MIND_SID but
    # failed to resolve MIND_AGENT (transient resolver miss). Without this
    # fallback the script exits 0 + empty stdout AND the caller
    # ('IF path returned: Read it' pattern) blindly proceeds with no compact.
    # Use python3 directly on _resolve_agent_from_sid.py rather than the
    # session-binding-read.sh wrapper: _platform.sh sets MSYS_NO_PATHCONV=1
    # which breaks nested bash invocations of Windows-form paths from a
    # subshell, but Python's argv parsing is unaffected.
    if [ -n "${MIND_SID:-}" ]; then
        AGENT_NAME="$(python3 "$CORE_ROOT/scripts/_resolve_agent_from_sid.py" "$MIND_SID" 2>/dev/null || true)"
        if [ -n "$AGENT_NAME" ]; then
            AGENT_DIR="$(agent_dir "$AGENT_NAME")"
            # Export so child invocations (aspirations-read.sh daemon roundtrips
            # via X-Mind-Agent header) see the resolved agent. Without this,
            # the fallback resolves AGENT_DIR locally but the daemon call
            # rejects with {"error":"agent_unset"} and the regen path fails.
            export MIND_AGENT="$AGENT_NAME"
        fi
    fi
    if [ -z "$AGENT_DIR" ]; then
        echo "[load-aspirations-compact] no agent bound (MIND_AGENT empty, MIND_SID=${MIND_SID:-unset} also unresolved), skipping" >&2
        exit 0
    fi
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
    #   COMPACT_SUMMARY  — BOUNDED projection for LLM Read-tool consumption.
    #                      Excludes terminal-status non-recurring goals, prunes
    #                      verbose fields, and then enforces a hard BYTE BUDGET
    #                      derived from the Read-tool cap.  (bravo
    #                      session 59 iter-18): full compact grew to 297KB
    #                      organically as 972 completed-non-recurring goals
    #                      accumulated across active aspirations; LLM Read of
    #                      the full file failed at 123K tokens.
    #
    # THE 2026-08-11 REGRESSION () AND WHY THE BOUND MOVED. The
    # original projection's only bound was "drop completed-non-recurring +
    # keep 15 fields", which is not a bound at all over a monotonically
    # growing store: the summary reached 672,651 B, 2.57x the 262,144 B cap,
    # so the caller contract ("IF path returned: Read it") could not complete.
    # Field-trimming cannot fix that — id+title+status ALONE measured
    # 360,889 B (1.38x cap) — so the projection is now row-budgeted, and the
    # budget is a FRACTION OF THE CAP rather than a constant tuned to today's
    # corpus, which is what makes it survive future growth.
    #
    # The projection moved OUT of this heredoc into _compact_summary.py. It
    # lived here for months at 2.5x the cap precisely because a heredoc cannot
    # be imported, so it could carry no regression test; the pin now lives in
    # tests/test_compact_summary_budget.py.
    #
    # indent=None + tight separators preserved (: indent=2 inflated
    # 234KB → 335KB purely via whitespace). SSOT for the FULL compact.
    #
    # Paths are passed via argv, never interpolated into the Python source
    # (guard-165).
    python3 -c "
import json, sys
sys.path.insert(0, sys.argv[5])
from _compact_summary import build_summary
w = json.load(open(sys.argv[1]))
a = json.load(open(sys.argv[2]))
merged = w + a
# Full canonical compact — no cap; Python consumers read THIS file.
with open(sys.argv[3], 'w', encoding='utf-8') as fh:
    json.dump(merged, fh, indent=None, separators=(',', ':'), ensure_ascii=True)
# LLM-facing bounded projection.
summary, stats = build_summary(merged)
with open(sys.argv[4], 'w', encoding='utf-8') as fh:
    json.dump(summary, fh, indent=None, separators=(',', ':'), ensure_ascii=True)
# Announce omissions on stderr. A projection that truncates quietly reads as a
# complete portfolio, which is worse than one that is loudly partial.
if stats['goals_omitted_for_budget']:
    sys.stderr.write(
        '[load-aspirations-compact] summary is BOUNDED: %d of %d eligible goals omitted '
        'to stay under the %d-byte budget (Read cap %d). Dropped by tier: %s. '
        'Per-aspiration counts are inline as goals_omitted. The FULL corpus is in %s.\n'
        % (stats['goals_omitted_for_budget'], stats['goals_eligible'],
           stats['budget_bytes'], stats['read_tool_cap'],
           json.dumps(stats['dropped_by_tier'], sort_keys=True), sys.argv[6]))
" "$COMPACT.tmp.w" "$COMPACT.tmp.a" "$COMPACT.tmp" "$COMPACT_SUMMARY.tmp" "$CORE_ROOT/scripts" "$COMPACT"
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
