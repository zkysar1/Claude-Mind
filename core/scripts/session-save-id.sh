#!/usr/bin/env bash
# SessionStart hook — save current Claude Code session UUID.
# Writes to mind/session/latest-session-id for runner identification.
# The stop hook compares this against running-session-id to determine
# if the current session is the autonomous loop runner.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
SESSION_DIR="$REPO_ROOT/mind/session"

# Skip if mind/session/ doesn't exist (UNINITIALIZED state)
[ -d "$SESSION_DIR" ] || exit 0

# Extract session_id from hook stdin JSON
SID=$(python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

echo "$SID" > "$SESSION_DIR/latest-session-id"
