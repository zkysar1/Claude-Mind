#!/usr/bin/env bash
# IRREDUCIBLY LOCAL -- per-Bash-call latency budget / hook / session-state critical path. Keep local: never add MCP or remote-service indirection here (a localhost daemon hop, where already present, is the maximum).
# output-style-gate.sh — Layer-B wrapper for /start (, guard-454, rb-629).
#
# Detects the active Claude Code output style from .claude/settings.local.json,
# then invokes world/scripts/output-style-mode-guard.sh with the detected style.
#
# Why this wrapper exists: /start needs ONE bash bullet, not a multi-line
# heredoc. This script is framework infrastructure (core/scripts/) and the
# domain-shared gate lives in world/scripts/ — keeping the split honors the
# 4-tier architecture (core = framework, world = domain).
#
# Usage:
#   bash core/scripts/output-style-gate.sh --mode autonomous
#   bash core/scripts/output-style-gate.sh --mode autonomous --override "<reason>"
#
# Exit codes (mirror the underlying gate):
#   0  No collision OR mode != autonomous OR style undetectable. Proceed.
#   2  Collision (autonomous + explanatory) AND no --override. /start MUST abort.
#   3  Collision but --override accepted. Audit logged. Proceed.
#
# Fail-open: if any precondition is missing (no _paths.sh, no gate script,
# no settings.local.json, no outputStyle key, no py launcher), exit 0.
# Layer A (Return Protocol) and Layer C (stop-hook trailing-text-detector)
# remain in effect — this gate is best-effort, not load-bearing.

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! source "$SCRIPT_DIR/_paths.sh" 2>/dev/null; then
    echo "[output-style-gate] WARN: _paths.sh not sourceable; layer-B disabled (fail-open)" >&2
    exit 0
fi

GATE="$WORLD_DIR/scripts/output-style-mode-guard.sh"
if [ ! -f "$GATE" ]; then
    echo "[output-style-gate] WARN: gate script missing at $GATE; layer-B disabled (fail-open)" >&2
    exit 0
fi

# Single source of truth: .claude/settings.local.json. If the file or the
# outputStyle key is absent, STYLE is empty → "unknown" → gate exits 0.
STYLE=$(py -3 -c "
import json, pathlib
p = pathlib.Path('.claude/settings.local.json')
if p.exists():
    try:
        s = (json.loads(p.read_text(encoding='utf-8')).get('outputStyle') or '').strip().lower()
        if s:
            print(s)
    except Exception:
        pass
" 2>/dev/null)

# CRITICAL: keep `exec` and keep --style as the LAST argument. The gate's
# argparse loop assigns the last --style value, so our injected style always
# wins over any caller-supplied --style — this wrapper is the authoritative
# style detector. Replacing `exec` with plain `bash` would mask the gate's
# exit code (silent layer-B bypass).
exec bash "$GATE" "$@" --style "${STYLE:-unknown}"
