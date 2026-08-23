#!/usr/bin/env bash
# bash-inject-misses-recent.sh — count recent entries in .bash-inject-misses.jsonl
#
# Paired with  (Investigate bash-agent-inject.sh injection robustness):
# the injector-side miss modes (timeout fail-open, no-binding) are
# architecturally locked, so the operational mitigation is making the miss
# log greppable + automatable. This script reports recent miss counts so
# /verify-learning can surface the failure class as a verdict.
#
# Output: JSON on stdout with shape:
#   {
#     "window_hours": 24,
#     "count": K,
#     "threshold_advisory": 5,
#     "threshold_alert": 20,
#     "verdict": "clean|advisory|alert",
#     "miss_log_path": "<absolute path>",
#     "miss_log_exists": true|false
#   }
# Exit 0 always (advisory check, never blocks the caller).
#
# Tunable via env:
#   MISS_WINDOW_HOURS         (default: 24)
#   MISS_THRESHOLD_ADVISORY   (default: 5)
#   MISS_THRESHOLD_ALERT      (default: 20)
#
# Test fixture support:
#   MISS_LOG_PATH=<path>      Override the miss-log path for tests.
set -u

WINDOW_HOURS="${MISS_WINDOW_HOURS:-24}"
ADVISORY="${MISS_THRESHOLD_ADVISORY:-5}"
ALERT="${MISS_THRESHOLD_ALERT:-20}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SCRIPT_DIR is core/scripts; PROJECT_ROOT is two levels up.
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# 2026-05-19 (plan v1 step 0.14): miss log relocated from PROJECT_ROOT/
# (`.bash-inject-misses.jsonl`) to core/logs/ canonical telemetry sink.
# Old root path checked only as fallback so any stragglers still surface
# until Phase 1.4 deletes them.
DEFAULT_LOG_PATH="$PROJECT_ROOT/core/logs/bash-inject-misses.jsonl"
LEGACY_LOG_PATH="$PROJECT_ROOT/.bash-inject-misses.jsonl"
if [[ -z "${MISS_LOG_PATH:-}" && ! -f "$DEFAULT_LOG_PATH" && -f "$LEGACY_LOG_PATH" ]]; then
    # Stragglers from before the relocation — keep reporting them until cleanup.
    LOG_PATH="$LEGACY_LOG_PATH"
else
    LOG_PATH="${MISS_LOG_PATH:-$DEFAULT_LOG_PATH}"
fi

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... Windows python3 misinterprets that as drive C: with a literal
# subdir c/, yielding FileNotFoundError on C:\c\...\<script>.py. Convert to
# Windows-native form before passing as a Python arg. Linux/macOS lack
# cygpath and fall through with LOG_PATH unchanged (POSIX paths work natively).
if command -v cygpath >/dev/null 2>&1; then
    LOG_PATH_NATIVE="$(cygpath -w "$LOG_PATH")"
else
    LOG_PATH_NATIVE="$LOG_PATH"
fi

# guard-165 (no bash-var interpolation into python -c): pass everything via env.
MISS_LOG_PATH="$LOG_PATH_NATIVE" \
MISS_WINDOW_HOURS="$WINDOW_HOURS" \
MISS_THRESHOLD_ADVISORY="$ADVISORY" \
MISS_THRESHOLD_ALERT="$ALERT" \
py -3 -c "
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

log_path = Path(os.environ['MISS_LOG_PATH'])
window_h = int(os.environ['MISS_WINDOW_HOURS'])
advisory = int(os.environ['MISS_THRESHOLD_ADVISORY'])
alert = int(os.environ['MISS_THRESHOLD_ALERT'])

now = datetime.now()
cutoff = now - timedelta(hours=window_h)

count = 0
log_exists = log_path.exists()
if log_exists:
    try:
        for line in log_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = rec.get('ts')
            if not ts_str:
                continue
            try:
                # ISO 8601 local timestamp, e.g., '2026-05-17T19:42:23'
                ts = datetime.strptime(ts_str, '%Y-%m-%dT%H:%M:%S')
            except ValueError:
                continue
            if ts >= cutoff:
                count += 1
    except OSError:
        # Read failure — treat as no data, fail-open
        pass

if count >= alert:
    verdict = 'alert'
elif count >= advisory:
    verdict = 'advisory'
else:
    verdict = 'clean'

print(json.dumps({
    'window_hours': window_h,
    'count': count,
    'threshold_advisory': advisory,
    'threshold_alert': alert,
    'verdict': verdict,
    'miss_log_path': str(log_path),
    'miss_log_exists': log_exists,
}, ensure_ascii=False))
"

exit 0
