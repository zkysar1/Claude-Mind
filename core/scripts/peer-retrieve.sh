#!/usr/bin/env bash
# peer-retrieve.sh -- retrieve across ALL registered worlds, not just this one.
#
# Usage: bash core/scripts/peer-retrieve.sh <query terms...> [--limit N]
#                                           [--include-archives] [--json]
#
# READ-ONLY by design: nothing a peer publishes is written into this world's
# stores. Every returned record keeps its origin_env (world-contract.md G5).
#
# Exit codes: 0 all worlds fully read | 2 usage | 3 PARTIAL -- at least one world
#             could not be fully read, so absence there is NOT evidence of absence.
#
# The rc=3 case is the point of the tool: peer reachability is box-dependent, and
# a cross-world retrieval that renders "unreachable" the same as "nothing found"
# manufactures confident negatives. See peer_retrieve.py's module docstring.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh"

#  cygpath conversion -- see peer-board-post.sh for the full rationale.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

exec python3 "$SCRIPT_DIR_NATIVE/peer_retrieve.py" "$@"
