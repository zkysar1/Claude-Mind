#!/usr/bin/env bash
# Interruptible sleep that checks for stop-loop signal every second.
# Usage: interruptible-sleep.sh <seconds>
# Exit 0 = completed or stop-loop detected. Exit 1 = bad args.
source "$(dirname "${BASH_SOURCE[0]}")/_paths.sh"

SECONDS_TO_SLEEP="${1:?Usage: interruptible-sleep.sh <seconds>}"
STOP_FILE="$AGENT_DIR/session/stop-loop"
STOP_REQ_FILE="$AGENT_DIR/session/stop-requested"

for (( i=0; i<SECONDS_TO_SLEEP; i++ )); do
  [ -f "$STOP_FILE" ] && exit 0
  [ -f "$STOP_REQ_FILE" ] && exit 0
  sleep 1
done
