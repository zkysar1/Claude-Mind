#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# Entry sentinel for hook-fire-audit — FIRST executable line, bash-builtin only.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/evolution-prepare-hook" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Write|Edit|MultiEdit] hook — Phase a of D1 (3-phase write path).
#
# Thin wrapper for evolution-prepare.py. Captures before-state when any
# Edit/Write touches one of the 4 evolution-tracked file kinds:
#   - <agent>/self.md        (file_kind=agent_self)
#   - world/program.md       (file_kind=program)
#   - .claude/skills/**/SKILL.md  (file_kind=skill_edit)
#   - .claude/rules/*.md     (file_kind=rule_edit)
#
# Untracked paths are silent no-ops.
#
# SAFETY: fail open. Never exits non-zero. Empty stdout + exit 0 = "approve
# with no mutation" per Claude Code's PreToolUse contract. See bible §5.4b
# and §14.7 (trigger model).

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/evolution-prepare.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
