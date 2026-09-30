#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/parked-body-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/parked-body-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash|Write|Edit|MultiEdit] hook (no argument) and UserPromptSubmit
# hook (argument `stamp`) — a PARKED worker Body does no work until its park
# check runs (). The decision and its reasons live in
# parked-body-gate.py.
#
# Thin bash wrapper. The Python body lives in parked-body-gate.py because a
# heredoc on `python -` would consume stdin before json.load runs (same reason
# as silent-zero-gate.sh).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Empty stdout + exit 0 =
# "approve with no mutation" per Claude Code's PreToolUse hook contract, and no
# stdout at all in `stamp` mode (a UserPromptSubmit hook's stdout joins the turn).

# Resolve this script's dir ONCE, without subprocesses when "$0" is absolute
# (the harness invokes hooks via $CLAUDE_PROJECT_DIR); same resolution as
# silent-zero-gate.sh.
_HOOK_DIR="${0%/*}"
case "$_HOOK_DIR" in
    /*|[A-Za-z]:/*) : ;;
    "$0"|"")        _HOOK_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 0 ;;
    *)              _HOOK_DIR="$(cd "$_HOOK_DIR" && pwd)" || exit 0 ;;
esac
source "$_HOOK_DIR/_paths.sh" 2>/dev/null || exit 0
source "$_HOOK_DIR/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/parked-body-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

# The gate's own stderr goes to a breakage log rather than /dev/null (as in
# silent-zero-gate.sh, ): a module-load failure would otherwise fail
# open silently. A non-empty core/logs/hook-fires/parked-body-gate.err marks a
# broken gate.
python3 "$SCRIPT_PATH" "$@" 2>>"$PROJECT_ROOT/core/logs/hook-fires/parked-body-gate.err"
exit 0
