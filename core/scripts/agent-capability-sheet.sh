#!/usr/bin/env bash
# agent-capability-sheet.sh — thin wrapper for agent-capability-sheet.py.
# Renders a per-agent capability sheet (Markdown, to stdout) as a live VIEW of
# the framework source-of-truth files. Read-only, local, non-daemon (mirrors the
# l1-skew-check.sh idiom). See the .py header for sources + the  design
# invariant (no second hand-maintained permission list).
#
# Usage: agent-capability-sheet.sh [<agent>|--list-sources]   (default agent: $MIND_AGENT)
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/agent-capability-sheet.py" "$@"
