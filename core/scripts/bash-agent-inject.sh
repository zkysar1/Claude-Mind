#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# PreToolUse[Bash] hook — auto-stamp MIND_AGENT on every Bash command.
#
# Thin bash wrapper. The Python body lives in bash-agent-inject.py because a
# heredoc on `python -` would consume stdin before json.load(sys.stdin) runs.
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.
#
# History: a 2026-04-19 attempt to rewrite this in pure bash + jq was reverted
# after discovering jq is not present in this machine's git-bash environment.
# The plan recorded that constraint at <USER_HOME>\.claude\plans\
# how-do-we-improve-wondrous-pine.md (Fix 3a aftermath). Installing jq
# (`pacman -S jq` in MSYS2 or dropping jq.exe into /usr/bin/) would unblock
# that rewrite if the cold-start latency becomes a real problem again.

# Source _paths.sh + _platform.sh: required on Windows so `python3` resolves to
# core/scripts/.python-shim/python3 (dispatches to `py`) AND so PROJECT_ROOT is
# in cygpath (C:/...) form — otherwise MSYS may hand python3 a /c/... path it
# cannot open. All other hooks in this repo follow the same two-source pattern.
# Do not "simplify" by removing either source.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/bash-agent-inject.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
