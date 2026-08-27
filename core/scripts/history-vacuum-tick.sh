#!/usr/bin/env bash
# domain-leak-exempt: reads Lodestar graveyard S3 identifiers from config and
# drives the S3-archived history-store GC; the cloud identifiers are functional.
#
# History-store vacuum cadence tick (g-115-2792-b; design g-115-2792-a in
# world/knowledge/tree/system/history-store-gc-cadence.md).
#
# The CAS-delta .history store's GC (_history_store.vacuum) had NO caller;
# orphaned blobs/patches accumulated (measured .history = 8.1G = 98% of
# WORLD_PATH). This tick invokes the archive-before-delete vacuum
# (history_vacuum_archive.py) on a 24h time-gate, per-box lock, and
# BACKGROUNDED so the ~114k-file walk never blocks iteration close.
#
# Wired into iteration-close.sh productivity-check maintenance sink beside
# evolution-stub-expiry.py / agent-watchdog.py --tick. Fail-open by design: a
# missed tick is fully recovered by the next iteration (idempotent + 24h gate),
# so it must never abort productivity-check. Self-scoped: .history is
# machine-local, so every box vacuums ITS OWN store; the per-box lock prevents
# concurrent vacuums when multiple agents share one box's .history.
#
# SAFE landing: history_vacuum.apply defaults false, so the backgrounded run
# ENUMERATES + reports only (no archive, no delete) until g-115-2792-c runs the
# positive control and flips apply=true.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

ENTRY="$SCRIPT_DIR/history_vacuum_archive.py"
LOG="${CORE_ROOT:-$SCRIPT_DIR/..}/logs/history-vacuum-tick.log"
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

# --- Config probe (single python read; fail-SAFE to disabled) -----------------
# Prints: "<enabled> <interval_hours> <reclaim_hours>". On ANY error the entry
# itself prints "false 24 2" (GC never runs on a bad config — a skipped GC is
# safe, an accidental delete is not).
probe="$(python3 "$ENTRY" --config-probe 2>/dev/null || echo "false 24 2")"
read -r ENABLED INTERVAL_HOURS RECLAIM_HOURS <<EOF
$probe
EOF
ENABLED="${ENABLED:-false}"
INTERVAL_HOURS="${INTERVAL_HOURS:-24}"
RECLAIM_HOURS="${RECLAIM_HOURS:-2}"

if [ "$ENABLED" != "true" ]; then
    exit 0
fi

# Guard non-integer config against the arithmetic below (fail-safe).
case "$INTERVAL_HOURS" in ''|*[!0-9]*) INTERVAL_HOURS=24 ;; esac
case "$RECLAIM_HOURS" in ''|*[!0-9]*) RECLAIM_HOURS=2 ;; esac
INTERVAL_MIN=$((INTERVAL_HOURS * 60))
RECLAIM_MIN=$((RECLAIM_HOURS * 60))

# --- Base dirs ----------------------------------------------------------------
# Default: world + meta each have their own .history. Explicit positional
# args override (an operator can target one base for the deliberate backlog
# drain; tests pass a tmp base). HISTORY_VACUUM_TICK_SYNC runs the vacuum
# inline instead of backgrounded (deterministic cadence/lock tests).
if [ "$#" -gt 0 ]; then
    BASES=("$@")
else
    BASES=("${WORLD_DIR:-}" "${META_DIR:-}")
fi

for BASE in "${BASES[@]}"; do
    [ -n "$BASE" ] || continue
    HIST="$BASE/.history"
    [ -d "$HIST" ] || continue

    LOCKDIR="$HIST/.vacuum.lock.d"
    STAMP="$HIST/.vacuum-last-run"

    # Stale-lock reclaim: a lock dir older than RECLAIM_HOURS is presumed dead
    # (the backgrounded run crashed before rmdir) and is reclaimed so GC is not
    # wedged forever.
    if [ -d "$LOCKDIR" ]; then
        if [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +"$RECLAIM_MIN" 2>/dev/null)" ]; then
            rm -rf "$LOCKDIR" 2>/dev/null || true
        fi
    fi

    # 24h time-gate: skip when the stamp exists and is younger than the interval.
    if [ -f "$STAMP" ] && [ -z "$(find "$STAMP" -maxdepth 0 -mmin +"$INTERVAL_MIN" 2>/dev/null)" ]; then
        continue
    fi

    # Acquire the per-box lock atomically. mkdir is atomic on POSIX + Windows
    # bash (portable, unlike flock). Failure => another vacuum holds it =>
    # clean no-op for this tick.
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        continue
    fi

    # Run the vacuum, then advance the stamp + release the lock. Backgrounded by
    # default so iteration close never blocks on the walk; the lock is released
    # by the subshell after the entry returns, and a crashed subshell is
    # recovered by the stale-lock reclaim above. HISTORY_VACUUM_TICK_SYNC runs
    # it inline (tests) so the stamp/lock lifecycle completes before return.
    if [ -n "${HISTORY_VACUUM_TICK_SYNC:-}" ]; then
        python3 "$ENTRY" --base-dir "$BASE" >>"$LOG" 2>&1 || true
        touch "$STAMP" 2>/dev/null || true
        rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
    else
        (
            python3 "$ENTRY" --base-dir "$BASE" >>"$LOG" 2>&1
            touch "$STAMP" 2>/dev/null || true
            rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
        ) &
    fi
done

exit 0
