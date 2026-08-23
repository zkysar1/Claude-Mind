#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook. Keep local: never add MCP or remote-service indirection here.
# PreToolUse[Bash] thin wrapper for git-restore-uncommitted-gate.py.
# ADVISORY, never blocking: warns when `git checkout`/`git restore` targets a
# path that currently carries uncommitted work (guard-1646 / guard-1838).
# Fail-open at every step; always exits 0.
#
# DELIBERATE DEVIATION from the sibling git-hook-bypass-gate.sh: python stderr is
# NOT piped to /dev/null here. This gate's advisory is written to stderr on
# purpose (the human-at-the-terminal channel, alongside the structured stdout
# payload that reaches the model), so blanket suppression would mute half the
# delivery. The traceback risk that motivates the sibling's `2>/dev/null` is
# closed at the source instead: the .py hardens its module-level import and
# carries a bottom catch-all, so nothing but the advisory can reach stderr.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/git-restore-uncommitted-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT
SCRIPT_PATH="$PROJECT_ROOT/core/scripts/git-restore-uncommitted-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0
python3 "$SCRIPT_PATH"
exit 0
