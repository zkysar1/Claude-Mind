#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path.
# Hook fire-audit sentinel
{ _HF_DIR="${BASH_SOURCE[0]%/*}/../.." ; mkdir -p "$_HF_DIR/core/logs/hook-fires" 2>/dev/null && : > "$_HF_DIR/core/logs/hook-fires/marker-placement-gate" 2>/dev/null ; unset _HF_DIR ; } 2>/dev/null || true

# PreToolUse[Write|Edit|MultiEdit] gate — Layer-B marker-placement check (Phase 5.7).
# Thin bash wrapper. Python body in marker-placement-gate.py emits the deny via
# hook_helpers.emit_deny (structured JSON on stdout + exit 0). This wrapper MUST
# always exit 0 — propagating a non-zero exit would be interpreted by Claude Code
# as a hook execution ERROR (fail-open silently), not a deny. Mirrors
# rule-vs-convention-gate.sh.
#
# SAFETY: fail open (exit 0) on ANY internal error so this gate cannot brick Edit/Write.
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh" 2>/dev/null || exit 0
source "$(cd "$(dirname "$0")" && pwd)/_platform.sh" 2>/dev/null || exit 0
export PROJECT_ROOT

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/marker-placement-gate.py"
[ -f "$SCRIPT_PATH" ] || exit 0

# Stderr suppressed because deny/approve communication happens via stdout JSON.
# The OVERRIDE stderr line (when an MARKER_PLACEMENT_OVERRIDE=... is accepted) is
# diagnostic only — surfaced via Claude Code's separate stderr stream when present.
python3 "$SCRIPT_PATH" 2>/dev/null
exit 0
