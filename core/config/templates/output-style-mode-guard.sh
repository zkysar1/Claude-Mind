#!/usr/bin/env bash
# output-style-mode-guard.sh — Layer-B gate against autonomous+explanatory loop death.
# Seeded into world/scripts/ by init-world.sh from core/config/templates/.
#
# Contract (called from core/scripts/output-style-gate.sh):
#   Args: --mode <mode> --style <style> [--override <reason>]
#   Exit 0: no collision → proceed
#   Exit 2: collision (autonomous + explanatory) AND no --override → refuse
#   Exit 3: collision but --override accepted → audit logged, proceed
#
# Why this gate exists: the autonomous loop's terminal output style at iteration
# close must be a tool call (Skill(aspirations)) per .claude/rules/return-protocol.md.
# Explanatory output style mandates trailing "✶ Insight" blocks AFTER tool calls
# — those land as text, the turn ends, and the loop dies silently (rb-629, guard-454).
#
# Edit this file in world/scripts/ to add domain-specific style/mode collisions.
# The core/scripts/output-style-gate.sh wrapper detects the style from
# .claude/settings.local.json and delegates here.

set -u

MODE=""
STYLE=""
OVERRIDE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --mode)     MODE="${2:-}"; shift 2 ;;
        --style)    STYLE="${2:-}"; shift 2 ;;
        --override) OVERRIDE="${2:-}"; shift 2 ;;
        *)          shift ;;
    esac
done

# Only the autonomous + explanatory combination is refused at this layer.
if [ "$MODE" != "autonomous" ] || [ "$STYLE" != "explanatory" ]; then
    exit 0
fi

if [ -z "$OVERRIDE" ]; then
    echo "[output-style-mode-guard] REFUSE: autonomous mode + explanatory output style is a documented loop killer (rb-629, guard-454, .claude/rules/return-protocol.md)." >&2
    echo "  Trailing '✶ Insight' blocks land as text after the terminal Skill(aspirations) call and kill the turn." >&2
    echo "  Switch with /output-style default first, then re-issue /start <agent>." >&2
    echo "  OR re-issue with --override-output-style \"<justification>\" to audit-log and proceed." >&2
    exit 2
fi

# --override accepted: audit log + exit 3
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORLD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LEDGER="$WORLD_DIR/output-style-overrides.jsonl"
TS=$(date +%Y-%m-%dT%H:%M:%S)
printf '{"timestamp":"%s","agent":"%s","mode":"%s","style":"%s","justification":"%s"}\n' \
    "$TS" "${MIND_AGENT:-unknown}" "$MODE" "$STYLE" "$OVERRIDE" \
    >> "$LEDGER" 2>/dev/null || true
echo "[output-style-mode-guard] OVERRIDE accepted; audit logged to $LEDGER" >&2
exit 3
