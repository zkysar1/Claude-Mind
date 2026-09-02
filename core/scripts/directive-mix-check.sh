#!/usr/bin/env bash
# Wrapper for directive_mix_check.py -- see that file's docstring for WHY.
# Sources _paths.sh so the world root resolves from the per-agent
# local-paths.conf. guard-3864: a bare `py -3` inherits STORAGE_BACKEND from
# settings.json but has no mappable world root, so it takes the own-cloud
# branch and silently reads the LOCAL MIRROR instead of the store of record.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  cygpath conversion -- see peer-board-post.sh for the full rationale.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/directive_mix_check.py" "$@"
