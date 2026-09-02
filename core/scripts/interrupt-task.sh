#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- loop re-entry critical path (checked every iteration at Phase -1.35).
# Thin exec passthrough so $MIND_AGENT / world roots resolve via _paths.sh, exactly as
# stranded-claim-sweep.sh does (guard-3864: a bare `py -3` here would miss the env).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"
cd "$PROJECT_ROOT"

#  cygpath conversion -- see peer-board-post.sh for the full rationale.
# This wrapper originally exec'd via "$CORE_ROOT/scripts/..." and so was INVISIBLE to
# test_cygpath_wrapper_pattern.py, whose discovery predicate greps the exec line for the
# literal token SCRIPT_DIR. CORE_ROOT is a plain `cd .. && pwd` with no nativization, so
# the Windows path-mangling bug was fully present and fully untested. Naming the variable
# SCRIPT_DIR is what puts this file back inside the regression net.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/interrupt_task.py" "$@"
