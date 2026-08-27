#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of
# core/logs/hook-fires/full-suite-imperative-gate = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/full-suite-imperative-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — delivers the run-full-suite imperative JIT, on the
# command rather than on a file read (goal , outcome #3). It is the
# half that makes path-scoping .claude/rules/run-full-suite-after-deep-code.md
# safe: a scoped rule is not re-injected after a compaction, but this fires on
# every suite invocation regardless of preamble state.
#
# ADVISORY ONLY. Always `permissionDecision: allow` — it never blocks a test
# run, it only says how to read the output.
#
# Thin bash wrapper. The Python body lives in the sibling .py because a heredoc
# on `python -` would consume stdin before json.load runs (same reason as
# gradle-tests-gate.sh and bash-path-resolution-hook.sh).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/full-suite-imperative-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
