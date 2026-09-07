#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook critical path. Keep local: never add MCP or remote-service indirection here.
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/embedded-block-extraction-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/embedded-block-extraction-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — ADVISORY for the hand-rolled embedded-block
# extraction shape (, closing guard-2222 at the tool layer after a
# third recurrence across a third agent).
#
# Thin bash wrapper. The Python body lives in
# embedded-block-extraction-gate.py because a heredoc on `python -` would
# consume stdin before json.load runs.
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract. NEVER propagate the python rc (guard-591).

# Resolve this script's dir ONCE. The harness invokes hooks via
# $CLAUDE_PROJECT_DIR, so "$0" is absolute and the parameter expansion below
# costs ZERO subprocesses. The cd+pwd fallback is kept verbatim for a relative
# or slash-less "$0", so resolution is unchanged in every case.
# Measured 2026-09-06 (alpha, DESKTOP-O91DLK2): `dirname` 172-752 ms and `pwd`
# 131-169 ms here, and this pair was evaluated TWICE per wrapper by 11 wrappers
# on EVERY Bash tool call.
_HOOK_DIR="${0%/*}"
case "$_HOOK_DIR" in
    /*|[A-Za-z]:/*) : ;;
    "$0"|"")        _HOOK_DIR="$(cd "$(dirname "$0")" && pwd)" || exit 0 ;;
    *)              _HOOK_DIR="$(cd "$_HOOK_DIR" && pwd)" || exit 0 ;;
esac
source "$_HOOK_DIR/_paths.sh" 2>/dev/null || exit 0
source "$_HOOK_DIR/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/embedded-block-extraction-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
