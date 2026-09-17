#!/usr/bin/env bash
# gs1-ownership.sh — which orphaned in-progress goals may THIS Body revert?
#
# The wrapper `aspirations-graceful-stop` Phase GS-1 calls. Exists because SKILL.md
# pseudocode must route Python through a .sh wrapper, never `bash <name>.py` (guard-350):
# bash parses the docstring as shell, every line errors, and a trailing `|| true` masks it
# into a silent exit 0 — a gate that appears to run and contributes zero signal.
#
# The DECISION is script-gated in gs1_ownership.py::decide (pure, branch-tested), so the
# rule cannot drift the way the prose cascade it replaces did. This wrapper owns only the
# plumbing: SID resolution and stdin pass-through. It WRITES NOTHING and mutates no goal —
# the caller performs the reverts from the `revert` list it prints.
#
# READ-ONLY BY CONTRACT. Do not add a revert here. Keeping the decision and the mutation in
# different processes is what lets the decision be tested without a store to damage.
#
# Usage:  aspirations-query.sh --goal-status in-progress --full | bash core/scripts/gs1-ownership.sh
#         bash core/scripts/gs1-ownership.sh --goals-file <path> [--sid <SID>]
#
# Prints the decision JSON: {revert[], skip[{goal_id,reason}], degraded, degraded_reason, counts{}}.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/_paths.sh" 2>/dev/null || true

# Default the SID from the environment, but let an explicit --sid win by passing argv
# through untouched — the module already treats a missing SID as "cannot prove ownership"
# and reverts nothing, so an unset MIND_SID degrades safely rather than silently.
SID_ARGS=()
case " $* " in
    *" --sid "*) ;;
    *) SID_ARGS=(--sid "${MIND_SID:-}") ;;
esac

#  fix: under Git Bash on Windows, $(cd ... && pwd) returns POSIX
# form /c/... that Windows python3 misreads as C:\c\... Convert to the
# native form before exec; Linux/macOS lack cygpath and fall through.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi
exec python3 "$SCRIPT_DIR_NATIVE/gs1_ownership.py" "${SID_ARGS[@]}" "$@"
