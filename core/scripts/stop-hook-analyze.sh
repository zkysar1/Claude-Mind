#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# stop-hook-analyze.sh
#
# Scans core/logs/stop-hook.log for recent consecutive BLOCKs per agent/session.
# When a session has >= STALL_THRESHOLD BLOCKs within STALL_WINDOW_SEC
# without an intervening ALLOW, appends a warning entry to
# <agent>/session/loop-stall-warnings.jsonl. Aspirations-precheck Phase 0.5a
# (or a downstream consumer) reads that file to feed reflection.
#
# Intended usage: recurring goal (interval_hours: 0.25) OR direct invocation
# during debugging. Idempotent — repeated runs may append duplicate entries
# for the same stall streak; consumers dedupe by (sid, first_block_ts).
#
# Parameters (env overrides):
#   STALL_THRESHOLD  (default: 3)    consecutive BLOCKs
#   STALL_WINDOW_SEC (default: 300)  window in seconds
#   LOG_PATH         (default: $PROJECT_ROOT/core/logs/stop-hook.log;
#                              falls back to legacy $PROJECT_ROOT/.stop-hook-log
#                              if new path absent and legacy still present)
#
# Flags:
#   --and-file   After writing warnings, invoke stall-goal-filer.sh for each
#                agent that received a new warning. Closes the observability-
#                to-backlog gap in one call. Off by default so the analyzer
#                stays single-responsibility when used alone.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

AND_FILE=false
for arg in "$@"; do
    case "$arg" in
        --and-file) AND_FILE=true ;;
        *) echo "stop-hook-analyze: unknown flag: $arg" >&2; exit 2 ;;
    esac
done

STALL_THRESHOLD="${STALL_THRESHOLD:-3}"
STALL_WINDOW_SEC="${STALL_WINDOW_SEC:-300}"
# 2026-05-19 (plan v1 step 0.15): default path moved to core/logs/. Legacy
# PROJECT_ROOT path checked as fallback so stragglers still surface until
# Phase 1.5 archives + deletes them.
DEFAULT_LOG_PATH="$PROJECT_ROOT/core/logs/stop-hook.log"
LEGACY_LOG_PATH="$PROJECT_ROOT/.stop-hook-log"
if [ -z "${LOG_PATH:-}" ]; then
    if [ -f "$DEFAULT_LOG_PATH" ]; then
        LOG_PATH="$DEFAULT_LOG_PATH"
    elif [ -f "$LEGACY_LOG_PATH" ]; then
        LOG_PATH="$LEGACY_LOG_PATH"
    else
        LOG_PATH="$DEFAULT_LOG_PATH"
    fi
fi

if [ ! -f "$LOG_PATH" ]; then
    echo "stop-hook-analyze: no log at $LOG_PATH" >&2
    exit 0
fi

# Capture Python output so --and-file can parse WROTE_AGENT markers afterwards.
# We print the captured output verbatim after the heredoc so the user-visible
# stream is identical to pre-flag behavior.
ANALYZE_OUT=$(python3 - "$LOG_PATH" "$STALL_THRESHOLD" "$STALL_WINDOW_SEC" "$PROJECT_ROOT" "$WORLD_DIR" <<'PY'
import sys, re, json, os
from datetime import datetime, timedelta

log_path, thr_s, win_s, proj_root, world_dir = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
# Field tolerance here is load-bearing, not defensive padding. stop-hook.sh has
# TWO BLOCK writers with DIFFERENT shapes: the main path emits
# `BLOCK sid=... agent=... runner_token=...` and the worker-net gate (added
# 2026-08-04 by ) emits `BLOCK gate=worker-net sid=... agent=...`
# (:484 and :388 as of 2026-08-10 — cites drift; grep `BLOCK ` in stop-hook.sh).
# A pattern requiring `sid=` immediately after `BLOCK` matched zero of the latter
# — measured on cc-07 2026-08-06: 3 BLOCK rows in the log, 0 matched, while a
# synthetic old-format row matched fine. That blindness is one-directional and
# therefore worse than a plain parse miss: pat_allow below tolerates intervening
# fields, so a worker-net ALLOW could CLEAR a streak that worker-net BLOCKs could
# never BUILD, and the analyzer kept printing "no loop stalls detected" — byte-
# identical to its healthy-fleet output. It ran 2 days unnoticed.
#
#  widened the one-optional-`gate=`-field special case to arbitrary
# intervening fields, because the writer side is under active development (the
# worker-net gate broke this consumer within 2 days of being added) and ANY
# second, third, or differently-named field restored the same asymmetry.
# `(?:\S+ )*?` consumes WHOLE space-separated fields. That is deliberately NOT
# pat_allow's `.*?`: a lazy `.*?` can halt mid-field on an embedded `sid=`
# substring, so on `BLOCK reason=missing-sid=x sid=S1 agent=alpha` it captures
# sid='x' — the wrong value, silently. rb-7110 is the general form (anchor on the
# discriminating MARKER — here the literal `BLOCK` token after the timestamp —
# never on a field that siblings also emit). Widening verified per guard-2201 on
# ONE snapshot of the live 484-line log: OLD 294 / NEW 294, REMOVED set empty,
# captures unchanged. Regression pin: core/scripts/tests/test_stop_hook_analyze_patterns.py
pat_block = re.compile(
    r"^(?P<ts>\S+) BLOCK (?:\S+ )*?sid=(?P<sid>\S+)(?: \S+)*? agent=(?P<agent>\S+)"
)
# DO NOT simplify pat_allow to drop the `sid=...` capture. Observer sessions
# produce ALLOW lines with `agent=X` but a different sid than the runner.
# Clearing the runner's streak on an observer ALLOW would hide real stalls.
pat_allow = re.compile(r"^(?P<ts>\S+) ALLOW .*?sid=(?P<sid>\S+).*? agent=(?P<agent>\S+)")

streaks = {}  # agent -> {sid, count, first_ts, last_ts}. Keyed by agent, scoped by sid.
warnings = {}

with open(log_path, "r", errors="replace") as f:
    for line in f:
        m = pat_block.match(line)
        if m:
            agent, sid, ts_s = m.group("agent"), m.group("sid"), m.group("ts")
            try:
                ts = datetime.fromisoformat(ts_s)
            except ValueError:
                continue
            s = streaks.get(agent)
            if s and s["sid"] == sid and (ts - s["first_ts"]) <= timedelta(seconds=win_s):
                s["count"] += 1
                s["last_ts"] = ts
            else:
                s = {"sid": sid, "count": 1, "first_ts": ts, "last_ts": ts}
            streaks[agent] = s
            if s["count"] >= thr_s:
                warnings[agent] = dict(s)
            continue
        m = pat_allow.match(line)
        if m:
            agent, allow_sid = m.group("agent"), m.group("sid")
            # Only clear if the ALLOW sid matches the sid we're tracking.
            # A sid-mismatch ALLOW is an observer session, not the runner.
            s = streaks.get(agent)
            if s and s["sid"] == allow_sid:
                streaks.pop(agent, None)

# Freshness gate — only warn on stalls whose last block is recent (within
# FRESHNESS_SEC of now). Prevents rediscovery of days-old streaks from a
# long-lived .stop-hook-log. Observed 2026-04-20: a warning with
# detected_at=2026-04-20T08:31 referenced first_block_ts=2026-04-17T12:30,
# three days stale. Default 3600s (1h); set FRESHNESS_SEC=0 to disable.
freshness_sec = int(os.environ.get("FRESHNESS_SEC", "3600"))
if freshness_sec > 0:
    now_dt = datetime.now()
    for agent in list(warnings.keys()):
        age_sec = (now_dt - warnings[agent]["last_ts"]).total_seconds()
        if age_sec > freshness_sec:
            del warnings[agent]

# False-positive suppression (): cross-reference each BLOCK streak
# against world/loop-death-detections.jsonl. The trailing-text-detector
# (invoked by stop-hook.sh) writes a record there ONLY when severity=high
# (real text-end stall pattern detected in transcript). Sleep-cycle BLOCKs
# during B7 backoff produce identical streak signatures BUT no detector
# entry (the transcript ends with a Bash tool call to interruptible-sleep.sh,
# not text). Loading the detections file once is cheap (<1KB typical) and
# avoids the false-positive class entirely.
#
# Bravo session 67 canonical incident: 17 BLOCKs in 35min during sleep-cycle
# (execution-diary had 0 phase entries 18:17:29-18:52:47), filed  as
# spurious Unblock goal. Without this gate, every B7 backoff session reaches
# the streak threshold (3 BLOCKs/300s matches normal idle-tick cadence) and
# accumulates false-positive Unblock goals.
detections_path = os.path.join(world_dir, "loop-death-detections.jsonl")
detections_by_agent = {}  # agent -> [datetime, ...] of detection timestamps
if os.path.exists(detections_path):
    try:
        with open(detections_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                a = rec.get("agent")
                ts_s = rec.get("timestamp")
                if not a or not ts_s:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_s)
                except ValueError:
                    continue
                detections_by_agent.setdefault(a, []).append(ts)
    except OSError:
        pass

for agent, latest in warnings.items():
    # Phase 2.5.D: agent dirs live under agents/ parent — was previously
    # os.path.join(proj_root, agent, "session") which resolved to a
    # non-existent path → every warning skipped with "no session dir".
    agent_dir = os.path.join(proj_root, "agents", agent, "session")
    if not os.path.isdir(agent_dir):
        print(f"stop-hook-analyze: no session dir for {agent}, skipping", file=sys.stderr)
        continue

    # Suppression check: if the BLOCK window has zero trailing-text detections,
    # the streak was almost certainly a sleep-cycle (interruptible-sleep.sh
    # tool calls, NOT text-end). Skip with a noop log line so the human-readable
    # output makes the suppression explicit.
    agent_detections = detections_by_agent.get(agent, [])
    window_start = latest["first_ts"] - timedelta(seconds=5)  # small slack for clock skew
    window_end = latest["last_ts"] + timedelta(seconds=5)
    matching = [t for t in agent_detections if window_start <= t <= window_end]
    if not matching:
        print(
            f"stop-hook-analyze: SUPPRESSED false-positive for {agent} "
            f"(count={latest['count']}, window {latest['first_ts'].isoformat()}..{latest['last_ts'].isoformat()}): "
            f"no loop-death-detections in window — likely sleep-cycle BLOCKs, not text-end stalls (g-001-18)"
        )
        continue

    out_path = os.path.join(agent_dir, "loop-stall-warnings.jsonl")
    warning = {
        "type": "loop_stall",
        "detected_at": datetime.now().isoformat(timespec="seconds"),
        "sid": latest["sid"],
        "consecutive_blocks": latest["count"],
        "first_block_ts": latest["first_ts"].isoformat(),
        "last_block_ts": latest["last_ts"].isoformat(),
        "threshold": thr_s,
        "window_sec": win_s,
        "note": "Agent turn ended with text output repeatedly. Check most recent sub-skill for missing Return Protocol terminal call.",
    }
    # Dedupe: skip if the last entry has the same sid+first_block_ts.
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", errors="replace") as f:
                lines = f.readlines()
            if lines:
                last = json.loads(lines[-1])
                if last.get("sid") == warning["sid"] and last.get("first_block_ts") == warning["first_block_ts"]:
                    print(f"stop-hook-analyze: warning for {agent} already recorded, skipping")
                    continue
        except (OSError, ValueError):
            pass
    with open(out_path, "a") as f:
        f.write(json.dumps(warning) + "\n")
    print(f"stop-hook-analyze: warning written for {agent} (count={latest['count']}) -> {out_path}")
    # Machine-readable marker consumed by --and-file below. Prefix is intentional
    # so grep stays tight even if the human-readable line format changes.
    print(f"WROTE_AGENT: {agent}")

if not warnings:
    print("stop-hook-analyze: no loop stalls detected")
PY
)

# Echo back to the user so behavior is identical to the pre --and-file script.
if [ -n "$ANALYZE_OUT" ]; then
    printf '%s\n' "$ANALYZE_OUT"
fi

if [ "$AND_FILE" = true ]; then
    # Invoke the filer once per agent that received a NEW warning in this run.
    # The WROTE_AGENT: prefix is emitted by the Python heredoc for each new
    # write (skips dedup-hit entries). Sort -u guards against duplicates if
    # the same agent got multiple warnings in one pass (currently impossible,
    # but cheap insurance).
    agents=$(printf '%s\n' "$ANALYZE_OUT" | awk '/^WROTE_AGENT: / {print $2}' | sort -u)
    if [ -n "$agents" ]; then
        while IFS= read -r a; do
            [ -z "$a" ] && continue
            echo "stop-hook-analyze: --and-file invoking stall-goal-filer for $a"
            MIND_AGENT="$a" bash "$CORE_ROOT/scripts/stall-goal-filer.sh" || \
                echo "stop-hook-analyze: filer failed for $a (non-fatal)" >&2
        done <<< "$agents"
    fi
fi
