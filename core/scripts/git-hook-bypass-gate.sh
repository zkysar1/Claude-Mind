#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/git-hook-bypass-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/git-hook-bypass-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — Layer-B half of the guard-901 defense ().
# Refuses git commit invocations that suppress the pre-commit hook chain:
# --no-verify (and -n short clusters), -c/--config-env core.hooksPath
# overrides, GIT_CONFIG_* env equivalents, and persistent `git config
# core.hooksPath` writes/unsets. A pre-commit hook cannot catch its own
# bypass (it does not run — rb-5390), so enforcement sits here.
#
# Thin bash wrapper. The Python body lives in git-hook-bypass-gate.py
# (mirrors bare-bash-authoring-gate.sh — a heredoc on `python -` would
# consume stdin before json.load runs).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.

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

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/git-hook-bypass-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
