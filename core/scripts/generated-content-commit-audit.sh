#!/usr/bin/env bash
# Thin wrapper for generated-content-commit-audit.py ().
#
# NOT a daemon wrapper: this is a standalone local tool, so
# .claude/rules/no-python-cli-fallback.md does not apply (that rule governs the
# 35 wrappers migrated to daemon endpoints). The wrapper exists only so bash
# callers get the `py -3` invocation right — a direct `python3 -c` from a Bash
# tool call hits the Windows Store stub (python-invocation.md).
#
# Usage:
#   generated-content-commit-audit.sh                  # 90d over AGENT_WRITE_PATH
#   generated-content-commit-audit.sh --days 30
#   generated-content-commit-audit.sh --json
#   generated-content-commit-audit.sh --repo <dir>     # REPLACES the roots
#   generated-content-commit-audit.sh --exit-on-hits   # rc=1 when findings
#
# Exit code is the verdict (0 clean / 1 findings-with---exit-on-hits /
# 2 operational error) — do NOT append an echo or a pipe after this call,
# either would replace it with the appended command's status (guard-1150,
# guard-888).
set -uo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$_SELF/../.." && pwd)}"
cd "$PROJECT_ROOT" || exit 2

# AGENT_WRITE_PATH is the repo-root list the audit enumerates. Source the
# resolver when it is not already set, so a bare invocation covers the whole
# corpus instead of silently scanning zero repos and reporting "clean".
if [ -z "${AGENT_WRITE_PATH:-}" ] && [ -f "$PROJECT_ROOT/core/scripts/_paths.sh" ]; then
    # shellcheck disable=SC1091
    . "$PROJECT_ROOT/core/scripts/_paths.sh" 2>/dev/null || true
fi

# EXPORT IS LOAD-BEARING, NOT DEFENSIVE (rb-2563). `_paths.sh` SETS
# AGENT_WRITE_PATH (indirectly, from the agent's local-paths.conf) but does NOT
# export it, so it is a shell variable and not an environment variable. `exec`
# passes the ENVIRONMENT, so without this line the Python child reads
# os.environ.get("AGENT_WRITE_PATH") == "" and enumerates ZERO repos.
#
# Measured 2026-08-11 on cc-03 while building this script: the bare wrapper
# printed `repos_scanned=0 findings=0`. Only the audit's own zero-repo coverage
# line distinguished that from a clean corpus — a sweep without that line would
# have reported a confident all-clear over nothing. Do not remove either half.
export AGENT_WRITE_PATH

exec py -3 "$PROJECT_ROOT/core/scripts/generated-content-commit-audit.py" "$@"
