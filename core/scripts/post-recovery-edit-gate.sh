#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path.
# Hook fire-audit sentinel
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/post-recovery-edit-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Write|Edit|MultiEdit] gate -- refuses framework edits when the
# bound agent is in (state=IDLE, mode=autonomous). Mirrors marker-placement-gate.sh.
#
# SAFETY: fail open (exit 0) on ANY internal error so this gate cannot brick Edit/Write.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/post-recovery-edit-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
