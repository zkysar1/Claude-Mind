#!/usr/bin/env bash
# Thin wrapper for embedded-python-audit.py ().
#
# NOT a daemon wrapper: this is a standalone local tool, so
# .claude/rules/no-python-cli-fallback.md does not apply (that rule governs the
# 35 wrappers migrated to daemon endpoints). The wrapper exists only so bash
# callers get the `py -3` invocation right — a direct `python3 -c` from a Bash
# tool call hits the Windows Store stub (python-invocation.md).
#
# Usage:
#   embedded-python-audit.sh                     # core/scripts + world/scripts
#   embedded-python-audit.sh --json
#   embedded-python-audit.sh --root <dir>        # REPLACES the defaults
#
# Exit code is the verdict (0 clean / 1 findings / 2 operational error) — do NOT
# append an echo or a pipe after this call, either would replace it with the
# appended command's status (guard-1150, guard-888).
set -uo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$_SELF/../.." && pwd)}"
cd "$PROJECT_ROOT" || exit 2

# WORLD_PATH is read by the audit to locate world/scripts. Source the resolver
# when it is not already exported, so a bare invocation still covers both trees
# rather than silently auditing half the corpus.
if [ -z "${WORLD_PATH:-}" ] && [ -f "$PROJECT_ROOT/core/scripts/_paths.sh" ]; then
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/core/scripts/_paths.sh" 2>/dev/null || true
fi

exec py -3 "$PROJECT_ROOT/core/scripts/embedded-python-audit.py" "$@"
