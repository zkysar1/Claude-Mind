#!/usr/bin/env bash
# run-scoped-suite — thin wrapper over run-scoped-suite.py.
#
# The FAST verification tier: runs only the tests that reference what changed.
# Self-serve — your own box, no quiet window, no tree lock, no queue, no peer
# coordination. Safe beside a live daemon and safe from a worker Body.
#
# It does NOT replace run-full-suite.sh. It makes the full run rare instead of
# mandatory-and-skipped. See run-full-suite-after-deep-code.md for when each applies.
#
# Verdict is TRI-STATE and an empty selection is NOT a pass:
#   0 PASS         a non-empty selection ran and every test passed
#   1 FAIL         a test failed or errored
#   2 INCONCLUSIVE empty selection / a changed file no test references / timeout
#   3 setup error
#
# Usage:
#   bash core/scripts/run-scoped-suite.sh                     # uncommitted working set
#   bash core/scripts/run-scoped-suite.sh --since origin/main # a branch's changes
#   bash core/scripts/run-scoped-suite.sh --changed core/scripts/foo.py
#   bash core/scripts/run-scoped-suite.sh --list-only --json   # dry run, no pytest
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

#  cygpath conversion — same reason as run-full-suite.sh: under Git Bash
# on Windows, $(cd ... && pwd) returns POSIX /c/... which Windows python3 reads as
# drive C: plus a literal c/ subdir. Linux/macOS lack cygpath and fall through.
if command -v cygpath >/dev/null 2>&1; then
    SCRIPT_DIR_NATIVE="$(cygpath -w "$SCRIPT_DIR")"
else
    SCRIPT_DIR_NATIVE="$SCRIPT_DIR"
fi

#  python3 resolution — _paths.sh is what puts core/scripts/.python-shim
# on PATH, and on Windows that shim IS `python3`. Without this, any invocation the
# PreToolUse bash-agent-inject hook does not reach (a nohup'd or backgrounded run)
# dies rc=127. Kept BELOW the cygpath probe for the same conservative ordering
# reason run-full-suite.sh documents.
if [ -f "$SCRIPT_DIR/_paths.sh" ]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/_paths.sh"
fi

# STORAGE_BACKEND=local is pinned INSIDE the .py (guard-955) rather than here, so
# it holds for every caller including a direct `python3 run-scoped-suite.py`.
# No tree lock is taken and none should be added: this tier is minutes, so the
# tree-moved class that voids the full suite does not apply, and a lock here would
# make it contend with the very peers it exists to stop blocking.
exec python3 -u "$SCRIPT_DIR_NATIVE/run-scoped-suite.py" "$@"
