#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Runner-heartbeat freshness probe (pure mtime).
#
# Two-way output on stdout (exit 0):
#   fresh  — mtime within runner_heartbeat.stale_minutes threshold
#   stale  — mtime older than threshold, OR heartbeat file missing.
#            Runner presumed crashed; recovery paths proceed.
#
# Usage: bash core/scripts/heartbeat-stale.sh
# Requires: MIND_AGENT set (via the PreToolUse[Bash] hook or explicit prefix).
#
# Why pure mtime (no writer-identity check): heartbeat-tick.sh is the single
# writer, called every iteration from Phase -0.5 AND every 60s during B7 waits
# from interruptible-sleep.sh. Any fresh mtime came from a legitimate writer.
# A writer-identity layer on top of mtime just adds a second surface that can
# disagree with the first, producing false positives (the 2026-04-21 orphaned-
# heartbeat incident). See core/config/conventions/compact-recovery.md.
#
# Missing heartbeat file = stale (fail open on the crashed-runner edge case).
# Same principle as /stop Step 5's "missing running-session-id counts as
# observer": no liveness signal → assume the worst and allow recovery.
#
# Missing `runner_heartbeat.stale_minutes` in aspirations.yaml is a misconfig,
# NOT a fail-open. The embedded Python writes the error to stderr and exits 1;
# `set -euo pipefail` then exits the script with empty stdout. Callers read
# stdout only — they see neither "stale" nor "fresh" and the user sees the
# stderr error. No caller branches on the exit code, so keep it that way.

set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh is required — the inline `python3 -c` block (line ~44) reads
# aspirations.yaml via `$CONFIG_DIR`, and the Windows-form path conversion
# lives here. Removing this line breaks YAML reads on Git Bash (/c/... →
# C:/...). DO NOT drop _platform.sh unless the inline Python is also removed.
source "$CORE_ROOT/scripts/_platform.sh"

HB="$AGENT_DIR/session/runner-heartbeat"

# Missing heartbeat = stale (fail-open for crashed-runner recovery).
if [ ! -f "$HB" ]; then
    echo stale
    exit 0
fi

# Read threshold from aspirations.yaml via python (no hardcoded default in
# shell — fails loud if the yaml block is missing, surfacing the misconfig
# instead of masking it with a magic number).
THRESHOLD=$(python3 -c "
import yaml, sys
with open(r'$CONFIG_DIR/aspirations.yaml', 'r', encoding='utf-8') as f:
    cfg = yaml.safe_load(f)
try:
    print(cfg['runner_heartbeat']['stale_minutes'])
except (KeyError, TypeError) as e:
    sys.stderr.write('ERROR: aspirations.yaml missing runner_heartbeat.stale_minutes block\n')
    sys.exit(1)
")

NOW=$(date +%s)
# stat -c %Y works on Git Bash (MSYS), Linux, and BSD-with-GNU-coreutils.
# BSD stat on macOS would need -f %m; this repo is tested on Git Bash + Linux.
MTIME=$(stat -c %Y "$HB")
AGE_SEC=$(( NOW - MTIME ))
THRESHOLD_SEC=$(( THRESHOLD * 60 ))

if [ "$AGE_SEC" -gt "$THRESHOLD_SEC" ]; then
    echo stale
else
    echo fresh
fi
