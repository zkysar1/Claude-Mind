#!/usr/bin/env bash
# Aspirations hot-store eviction cadence tick (2026-08-14, operator-directed
# S3 cost-down session — threshold approved verbatim: "go 3").
#
# aspirations-evict-completed.py is the proven, metric-neutral evictor
# (census indirection, merge-safe via archived_census.evicted_ids tombstones
# ) — but it shipped with NO call site and a 45-day default that delivered 0.02%
# of the measured saving (). First hand-run 2026-08-14T12:54 cut
# world/aspirations.jsonl 36.6MB -> 16.6MB (54%, 4,809 goals). Without a
# cadence the file regrows ~2.9MB/day and the saving decays.
#
# Same LOCAL-tick contract as history-vacuum-tick.sh (the sibling this
# mirrors): 24h time-gate via a machine-local stamp, per-box lock,
# BACKGROUNDED run, fail-open everywhere (a missed tick is fully recovered
# by the next iteration). Multi-box overlap is harmless by design: eviction
# is idempotent (only newly-aged terminal goals are eligible) and the merge
# layer drops stale-replica resurrections at the tombstone set.
#
# The operator-offload-gate correctly refused a recurring GOAL for this work
# (deterministic + clocked + checkable => no LLM iteration should burn on
# it); this tick is the sanctioned zero-LLM shape, per the vacuum precedent.
#
# NO RECORD-LEVEL RECOVERY — this header claimed "recoverable via .history 7d +
# lifecycle-exempt graveyard" until 2026-08-18; both halves were false (this
# script's evictor has ZERO graveyard code — that belongs to
# history_vacuum_archive.py over a different set — and ".history recovery" is the
# sentence the evictor's own header retracts as infeasible). Eviction is a
# one-way drop of goal records, by design. Do not re-add a recovery claim here.
# Measurement, consequences, and the age_days-vs-lookback finding: the
# § aspirations_eviction comment block in the config named below ().
#
# Config: core/config/aspirations.yaml § aspirations_eviction
#   enabled: true|false   interval_hours: 24   age_days: 3   apply: true|false
# Fail-SAFE: any config-read error resolves to disabled — eviction never
# runs on a bad config (a skipped tick is recoverable; a wrong one is not).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

EVICTOR="$SCRIPT_DIR/aspirations-evict-completed.py"
LOG="${CORE_ROOT:-$SCRIPT_DIR/..}/logs/aspirations-evict-tick.log"
STAMP="${CORE_ROOT:-$SCRIPT_DIR/..}/logs/.aspirations-evict-last-run"
LOCKDIR="${CORE_ROOT:-$SCRIPT_DIR/..}/logs/.aspirations-evict-lock"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# --- Config probe (single python read; fail-SAFE to disabled) ----------------
# guard-879: forward resolved paths explicitly; heredoc python has no __file__.
export MIND_WORLD="${WORLD_PATH:-}" MIND_META="${META_PATH:-}"
probe="$(TICK_CFG="$PROJECT_ROOT/core/config/aspirations.yaml" python3 - <<'PY' 2>/dev/null || echo "false 24 3 false"
import os, yaml
try:
    cfg = yaml.safe_load(open(os.environ["TICK_CFG"], encoding="utf-8"))
    ev = (cfg or {}).get("aspirations_eviction") or {}
    print(str(bool(ev.get("enabled", False))).lower(),
          int(ev.get("interval_hours", 24)),
          int(ev.get("age_days", 3)),
          str(bool(ev.get("apply", False))).lower())
except Exception:
    print("false 24 3 false")
PY
)"
read -r ENABLED INTERVAL_HOURS AGE_DAYS APPLY <<EOF
$probe
EOF
ENABLED="${ENABLED:-false}"; INTERVAL_HOURS="${INTERVAL_HOURS:-24}"
AGE_DAYS="${AGE_DAYS:-3}"; APPLY="${APPLY:-false}"

[ "$ENABLED" = "true" ] || exit 0

# --- 24h time-gate (machine-local stamp) -------------------------------------
if [ -f "$STAMP" ]; then
    now=$(date +%s)
    last=$(stat -c %Y "$STAMP" 2>/dev/null || stat -f %m "$STAMP" 2>/dev/null || echo 0)
    elapsed=$(( now - last ))
    [ "$elapsed" -ge $(( INTERVAL_HOURS * 3600 )) ] || exit 0
fi

# --- Per-box lock (stale locks reclaimed after 2h, mirroring the vacuum) -----
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCKDIR" 2>/dev/null || stat -f %m "$LOCKDIR" 2>/dev/null || date +%s) ))
    if [ "$lock_age" -ge 7200 ]; then
        rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
        mkdir "$LOCKDIR" 2>/dev/null || exit 0
    else
        exit 0
    fi
fi

APPLY_FLAG=""
[ "$APPLY" = "true" ] && APPLY_FLAG="--apply"

# --- Backgrounded run: world store, then the bound agent's own store ---------
(
    {
        echo "--- $(date +%Y-%m-%dT%H:%M:%S) evict tick (age_days=$AGE_DAYS apply=$APPLY agent=${MIND_AGENT:-unset}) ---"
        python3 "$EVICTOR" --source world --age-days "$AGE_DAYS" $APPLY_FLAG 2>&1 | tail -3
        python3 "$EVICTOR" --source agent --age-days "$AGE_DAYS" $APPLY_FLAG 2>&1 | tail -3
    } >>"$LOG" 2>&1
    touch "$STAMP" 2>/dev/null || true
    rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
) &

exit 0
