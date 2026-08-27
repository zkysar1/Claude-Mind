#!/usr/bin/env bash
# framework-pull.sh — adopting-side executor for the promotion chain.
#
# Thin wrapper over core/scripts/framework_pull.py. See
# core/config/conventions/pull-promotion.md for the protocol this implements.
#
#   bash core/scripts/framework-pull.sh --source-repo ../claude-mind            # plan (default)
#   bash core/scripts/framework-pull.sh --source-repo ../claude-mind --json
#   bash core/scripts/framework-pull.sh --source-repo ../claude-mind --adopt
#
# Exit: 0 ok | 2 blocked | 3 rolled back | 1 error.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$SCRIPT_DIR/_paths.sh"

# : under Git Bash on Windows, $(cd ... && pwd) yields POSIX /c/...,
# which Windows python3 reads as drive C: plus a literal subdir c/ — so the
# interpreter dies with FileNotFoundError on C:\c\...\framework_pull.py. Convert
# via cygpath -w where it exists; elsewhere fall through unchanged (POSIX paths
# work natively). Only the python3 ARGUMENT needs this — bash itself resolves
# /c/... fine, which is why the `. "$SCRIPT_DIR/_paths.sh"` source above is
# correct as written.
# This wrapper is the ADOPTING side of the promotion chain, so it runs on
# downstream Minds — including the fleet's Windows boxes, where the raw form
# would fail on the very machines the pull exists to serve.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/framework_pull.py" "$@"
