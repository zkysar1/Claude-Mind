#!/usr/bin/env bash
# core/scripts/outcome-observation-run.sh
#
# Wrapper for world/scripts/outcome-metrics-collect.sh. Resolves the script
# via $WORLD_DIR absolute path (the previous Step 8.12 invocation used the
# relative form `bash world/scripts/...` which fails because `world/` is a
# virtual prefix mapping to an external user-supplied path — see
# .claude/rules/path-resolution.md). That relative-path bug caused the
# 2026-04-24 to 2026-05-14 silent staleness window.
#
# Three responsibilities:
#   1. Resolve the collector via $WORLD_DIR — restores the cadence wiring.
#   2. Append an audit entry to core/logs/outcome-observation-runs.jsonl
#      (replaces the original `2>/dev/null || true` silent-swallow).
#   3. Return exit 0 unconditionally — fail-open at wrapper level so
#      state-update never aborts on collector errors.
#
# Usage:
#   outcome-observation-run.sh <goal_id> <outcome_class>
# Called from:
#   - world/conventions/outcome-observation.md Step 1 (deep-outcome path)
#   - aspirations-state-update Step 4.5 (opportunistic refresh for
#     routine outcomes when metrics mtime > 24h)

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/_paths.sh"

GOAL_ID="${1:-unknown}"
OUTCOME_CLASS="${2:-unknown}"
AGENT="${MIND_AGENT:-unknown}"
LOG_FILE="$PROJECT_ROOT/core/logs/outcome-observation-runs.jsonl"
COLLECTOR="$WORLD_DIR/scripts/outcome-metrics-collect.sh"
METRICS_FILE="$WORLD_DIR/outcome-metrics.yaml"

mkdir -p "$(dirname "$LOG_FILE")"
START_TS=$(date +%Y-%m-%dT%H:%M:%S)

if [ ! -f "$COLLECTOR" ]; then
    EXIT=127
    STDERR_HEAD="collector script not found at $COLLECTOR"
    METRICS_MTIME="absent-collector"
else
    STDERR_TMP=$(mktemp)
    bash "$COLLECTOR" 2>"$STDERR_TMP" >/dev/null
    EXIT=$?
    STDERR_HEAD=$(head -1 "$STDERR_TMP" | tr -d '\n\r' | cut -c1-200)
    rm -f "$STDERR_TMP"
    if [ -f "$METRICS_FILE" ]; then
        METRICS_MTIME=$(stat -c %y "$METRICS_FILE" | cut -d'.' -f1)
    else
        METRICS_MTIME="absent"
    fi
fi

# Operator-visible warning on failure (). The audit
# entry below records exit + stderr_head, but a FILE is not the same visibility
# as a line in the loop's captured output. iteration-close.sh previously called
# the collector directly and warned on failure; routing it through this wrapper
# would have SILENTLY dropped that warning, because this wrapper exits 0 by
# contract. Emitting it here preserves the warning for that caller and adds it
# for every other one, without touching the exit-0 contract state-update depends
# on. Trading a visible warning for a structured log entry would have been a
# quiet downgrade of exactly the signal this wrapper exists to provide.
if [ "$EXIT" -ne 0 ]; then
    echo "[outcome-observation-run] WARN: collector failed rc=$EXIT (non-fatal;" \
         "outcome-metrics.yaml not advanced). stderr: ${STDERR_HEAD:-<none>}" >&2
fi

# Values passed via env (guard-165: never interpolate bash vars into Python source).
# Single python3 invocation per python-invocation.md rule 3: inside a .sh wrapper
# that has sourced _paths.sh, the shim is on PATH and python3 is canonical.
OBS_TS="$START_TS" \
OBS_AGENT="$AGENT" \
OBS_GOAL="$GOAL_ID" \
OBS_CLASS="$OUTCOME_CLASS" \
OBS_EXIT="$EXIT" \
OBS_MTIME="$METRICS_MTIME" \
OBS_STDERR="$STDERR_HEAD" \
OBS_LOG="$LOG_FILE" \
python3 -c '
import json, os
entry = {
    "ts": os.environ["OBS_TS"],
    "agent": os.environ["OBS_AGENT"],
    "goal_id": os.environ["OBS_GOAL"],
    "outcome_class": os.environ["OBS_CLASS"],
    "exit": int(os.environ["OBS_EXIT"]),
    "metrics_mtime_after": os.environ["OBS_MTIME"],
    "stderr_head": (os.environ.get("OBS_STDERR") or None) if int(os.environ["OBS_EXIT"]) != 0 else None,
}
with open(os.environ["OBS_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(entry) + "\n")
'

# Fail-open at wrapper level: state-update never aborts on collector errors.
# Audit telemetry already captured above. Do not change this without revisiting
# the cadence-guarantee contract in world/conventions/outcome-observation.md.
exit 0
