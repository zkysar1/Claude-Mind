#!/usr/bin/env bash
# Promotion preflight drift gate — thin wrapper.
#
# Standalone READ-ONLY cross-repo audit tool (NOT a daemon endpoint, so no
# runtime/daemon dependency and no Python-CLI-fallback concern — it never
# touches agent state). Run it BEFORE any framework promotion (overwrite of a
# downstream repo) to surface content the target leads on that a blind mirror
# would clobber. See core/scripts/promotion-preflight.py for the full contract.
#
# Usage:
#   bash core/scripts/promotion-preflight.sh --source <incoming_repo> --target <repo_to_overwrite> [--strict] [--json]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/promotion-preflight.py"
# Windows: `py -3 <file>` avoids the Microsoft Store python3 stub (see
# core/config/conventions/python-invocation.md). Fall back to python3 (shimmed
# by the PreToolUse hook) only if the py launcher is absent.
if command -v py >/dev/null 2>&1; then
  exec py -3 "$SCRIPT" "$@"
else
  exec python3 "$SCRIPT" "$@"
fi
