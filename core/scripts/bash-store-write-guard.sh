#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/bash-store-write-guard
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/bash-store-write-guard" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — refuse ad-hoc commands that write a governed
# JSONL/YAML store directly (redirect, tee, sed -i, rm, cp/mv into it, inline
# Python opening it for writing). The store's framework script is the ONLY
# writer. Canonical incident: a worker Body hand-wrote status "done" into
# agents/<agent>/aspirations.jsonl (a downstream deployment, 2026-08-29). Body lives in
# bash-store-write-guard.py; this wrapper mirrors silent-zero-gate.sh.
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation".

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/bash-store-write-guard.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>>"$PROJECT_ROOT/core/logs/hook-fires/bash-store-write-guard.err"
exit 0
