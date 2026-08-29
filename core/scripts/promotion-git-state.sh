#!/usr/bin/env bash
# Promotion git-state integration — thin wrapper.
#
# Standalone cross-repo git tool (NOT a daemon endpoint, so no runtime/daemon
# dependency and no Python-CLI-fallback concern — it never touches agent state).
# Sibling of promotion-preflight.sh, which audits CONTENT drift; this audits GIT
# STATE at the two ends of a promotion. See promotion-git-state.py for the full
# contract and the step classification (guard-365).
#
# Usage:
#   bash core/scripts/promotion-git-state.sh freshness  --target <clone> [--upstream origin/main] [--apply] [--json]
#   bash core/scripts/promotion-git-state.sh postflight --target <clone> [--branch promote/vX.Y.Z] \
#        [--pr <url|number>] [--tag vX.Y.Z] [--plant-clone <path>] [--also-confirm <repo>]... [--apply] [--json]
#
# Exit: 0 clean · 2 action needed (unsafe ff / outstanding cleanup / obligations) · 3 unreadable
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/promotion-git-state.py"
# Windows: `py -3 <file>` avoids the Microsoft Store python3 stub (see
# core/config/conventions/python-invocation.md). Fall back to python3 (shimmed
# by the PreToolUse hook) only if the py launcher is absent.
if command -v py >/dev/null 2>&1; then
  exec py -3 "$SCRIPT" "$@"
else
  exec python3 "$SCRIPT" "$@"
fi
