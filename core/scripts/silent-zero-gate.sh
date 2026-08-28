#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit () — FIRST executable line,
# bash-builtin only, fail-open. mtime of core/logs/hook-fires/silent-zero-gate
# = last fire of this hook.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/silent-zero-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Bash] hook — Layer A of the silent-zero defense (goal ).
# Refuses `<framework wrapper> | parse(read() or '[]')` pipelines, which score a
# FAILED call as a legitimate zero because stdout is empty either way and the
# exit status is never read. The predicate lives in _silent_zero_predicate.py
# (single source of truth, shared with the Layer C audit).
#
# Thin bash wrapper. The Python body lives in silent-zero-gate.py because a
# heredoc on `python -` would consume stdin before json.load runs (same reason
# as bash-path-resolution-hook.sh and gradle-tests-gate.sh).
#
# SAFETY: fail open on ANY error. Never exits non-zero. Never emits malformed
# JSON. Empty stdout + exit 0 = "approve with no mutation" per Claude Code's
# PreToolUse hook contract.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/silent-zero-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

# D3 (): capture the gate's OWN stderr to a breakage log instead of
# discarding it. A module-load failure (e.g. a renamed import) escapes
# silent-zero-gate.py's in-main try/except and would otherwise vanish into
# /dev/null while `exit 0` fails open -- a dead gate that silently approves and
# reports nothing. stdout (the hook decision channel) is untouched, so the
# PreToolUse contract and the fail-open exit are unchanged; a non-empty
# core/logs/hook-fires/silent-zero-gate.err now marks a broken gate.
python3 "$SCRIPT_PATH" 2>>"$PROJECT_ROOT/core/logs/hook-fires/silent-zero-gate.err"
exit 0
