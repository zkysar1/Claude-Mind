#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/gradle-tests-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/gradle-tests-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — Layer A of the gradle --tests uppercase-package
# defense (goal ). Refuses `--tests 'MyPackage.MyTest'` patterns that
# Gradle silently resolves to zero tests. The rule file
# (.claude/rules/gradle-tests-pattern.md) is Layer B; gradle-tests-audit.py is
# the Layer C detective over the committed corpus.
#
# Thin bash wrapper. The Python body lives in gradle-tests-gate.py because a
# heredoc on `python -` would consume stdin before json.load runs (same reason
# as bash-path-resolution-hook.sh and bare-bash-authoring-gate.sh).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/gradle-tests-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
