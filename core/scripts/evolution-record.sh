#!/usr/bin/env bash
# Entry sentinel for hook-fire-audit — FIRST executable line, bash-builtin only.
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/evolution-record-hook" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PostToolUse[Write|Edit|MultiEdit] hook — Phase a of D1, finalization step.
#
# Thin wrapper for evolution-record.py. Reads the sidecar that
# evolution-prepare.sh wrote, computes after_hash, classifies change,
# writes a stub entry (status=awaiting_completion) to the appropriate
# event stream:
#   - world/self-evolution.jsonl
#   - world/program-evolution.jsonl
#   - world/skill-evolution.jsonl
#   - world/rule-evolution.jsonl
#
# If no sidecar exists (untracked path or Pre silent-failed), no-op.
#
# SAFETY: fail open. PostToolUse hooks can never block (edit already
# applied). See bible §5.4b and §14.10 worked example.

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/evolution-record.py"
[ -f "$SCRIPT_PATH" ] || exit 0

python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
