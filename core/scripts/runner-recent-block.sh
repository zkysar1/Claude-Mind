#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# runner-recent-block.sh — multi-signal liveness probe ().
#
# Returns 0 if the agent has had a recent stop-hook BLOCK in the last
# RECENT_WINDOW_SEC seconds (default 300 = 5 min), 1 if not.
#
# Used by recovery-gate.sh Path A and start/SKILL.md "RUNNING + autonomous"
# auto-recovery branch as a secondary liveness signal. Heartbeat staleness
# alone is too weak: transient platform issues (e.g., Claude Code 2.1.133
# stop-hook timeouts at 8.4s when normal is 2.5s) can stale the heartbeat
# briefly even though the runner is alive. A recent BLOCK proves the
# stop-hook fired AND the runner survived AND the loop re-entered — three
# events that don't happen on a dead runner.
#
# Origin: 2026-05-09 cross-binding stomp (rb-encoded under ).
# bravo's ecaf3b27 runner had a BLOCK at 06:33:42, then /start at 06:34:xx
# stomped it because heartbeat was briefly stale. With this probe, /start
# would have observed the recent BLOCK and held back.
#
# Window choice (300s default): long enough to absorb one or two hook
# timeout cycles, short enough that genuinely-crashed runners get recovered
# within ~5 minutes. Tunable via RECENT_WINDOW_SEC env var if a domain
# needs a different default.
#
# Usage:
#   bash core/scripts/runner-recent-block.sh <agent-name>
# Exit codes:
#   0 = recent BLOCK found (runner appears alive — suppress recovery)
#   1 = no recent BLOCK (runner appears dead — recovery may proceed)
#   2 = invalid arguments

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

AGENT="${1:-}"
[ -n "$AGENT" ] || { echo "usage: runner-recent-block.sh <agent>" >&2; exit 2; }

RECENT_WINDOW_SEC="${RECENT_WINDOW_SEC:-300}"
# 2026-05-19 (plan v1 step 0.15): stop-hook log relocated to core/logs/.
# Check new path first; fall back to legacy PROJECT_ROOT path for stragglers
# until Phase 1.5 archives + deletes them.
if [ -f "$PROJECT_ROOT/core/logs/stop-hook.log" ]; then
    LOG="$PROJECT_ROOT/core/logs/stop-hook.log"
else
    LOG="$PROJECT_ROOT/.stop-hook-log"
fi
DIARY="$(agent_dir "$AGENT")/session/execution-diary.jsonl"
NOW_EPOCH="$(date +%s)"

# ── Signal 1: most-recent stop-hook BLOCK for this agent ──
# ALLOW entries don't count — they signal sid-mismatch (loop dying), not liveness.
BLOCK_AGE="-1"
if [ -f "$LOG" ]; then
    LAST_BLOCK="$(grep -E "BLOCK .*agent=$AGENT($| )" "$LOG" 2>/dev/null | tail -1)"
    if [ -n "$LAST_BLOCK" ]; then
        BLOCK_TS="${LAST_BLOCK%% *}"
        # date -d isn't reliable across MSYS / WSL / GNU date — route through python.
        BLOCK_EPOCH="$(py -3 -c "
import sys, datetime
try:
    print(int(datetime.datetime.strptime(sys.argv[1], '%Y-%m-%dT%H:%M:%S').timestamp()))
except Exception:
    print(0)
" "$BLOCK_TS" 2>/dev/null)"
        # Conservative-on-error (, mirrors rb-762 pattern in recovery-gate.sh):
        # distinguish parse-failure from no-LOG. Empty BLOCK_EPOCH (python missing) or
        # "0" (parse threw) means we couldn't decide — treat as alive (exit 0,
        # suppress recovery), don't declare no-recent-block. Inverts prior
        # fail-CLOSED behavior of `... || exit 1` which inverted the
        # multi-signal-defensive intent of .
        if [ -z "$BLOCK_EPOCH" ] || [ "$BLOCK_EPOCH" = "0" ]; then
            echo "runner-recent-block: BLOCK_TS parse failed for '$BLOCK_TS' — treating as alive (conservative)" >&2
            exit 0
        fi
        BLOCK_AGE=$((NOW_EPOCH - BLOCK_EPOCH))
    fi
fi

# ── Signal 2: execution-diary mtime ( second independent signal) ──
# Stop-hook can be silent for legitimate reasons (long single-turn iteration,
# hung autocompact mid-write) while the loop is alive AND the diary is
# being appended every phase. Diary mtime is independently produced by the
# runner — neither signal masks the other. OR-aggregation: either alone is
# sufficient liveness evidence; both are independent samples of "the
# runner ran something recently."
DIARY_AGE="-1"
if [ -f "$DIARY" ]; then
    # Cross-platform mtime: stat -c %Y on GNU, stat -f %m on BSD/macOS.
    # Route through python for portability with the BLOCK_EPOCH path.
    DIARY_EPOCH="$(py -3 -c "
import os, sys
try:
    print(int(os.path.getmtime(sys.argv[1])))
except Exception:
    print(0)
" "$DIARY" 2>/dev/null)"
    if [ -n "$DIARY_EPOCH" ] && [ "$DIARY_EPOCH" != "0" ]; then
        DIARY_AGE=$((NOW_EPOCH - DIARY_EPOCH))
    fi
fi

# ── OR-aggregation ──
# Either signal alone within window = alive. Mirrors the multi-signal defensive
# intent of . Negative ages (-1) mean signal absent — does not
# contribute to the liveness conclusion. Both absent = exit 1 (no signal).
if [ "$BLOCK_AGE" -ge 0 ] && [ "$BLOCK_AGE" -lt "$RECENT_WINDOW_SEC" ]; then
    exit 0
fi
if [ "$DIARY_AGE" -ge 0 ] && [ "$DIARY_AGE" -lt "$RECENT_WINDOW_SEC" ]; then
    exit 0
fi
exit 1
