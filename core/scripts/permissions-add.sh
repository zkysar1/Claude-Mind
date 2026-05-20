#!/usr/bin/env bash
# permissions-add.sh — Sanctioned wrapper that adds external-path permissions
# to .claude/settings.local.json.
#
# This is the user-authorized maintenance path for the constitutional anchor
# (rb-931, CLAUDE.md "two-file settings rule"). The LLM cannot Edit/Write/
# MultiEdit .claude/settings.local.json directly because the file's own
# deny[] hard-blocks those tool calls. Bash-level writes via this script
# ARE permitted — the deny patterns match Claude tool names, not the
# underlying filesystem.
#
# Resolves WORLD_DIR + META_DIR from the bound agent's local-paths.conf via
# _paths.sh — caller does not pass paths explicitly. PROJECT_ROOT is also
# resolved from _paths.sh. Requires MIND_AGENT to be set (PreToolUse[Bash]
# hook auto-injects this from the session binding written by /start A2).
#
# Usage:
#   bash core/scripts/permissions-add.sh
#
# Exit codes:
#   0 — success (created or merged idempotently)
#   2 — required state missing (MIND_AGENT unset, paths unresolved)
#   3 — existing settings.local.json is malformed (refused to clobber)
#   4 — Python helper missing or Python launcher unavailable
#
# Origin: 2026-05-19 zeta /start audit, finding #1.

set -uo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

if [ -z "${WORLD_DIR:-}" ] || [ -z "${META_DIR:-}" ]; then
    echo "ERROR: WORLD_DIR or META_DIR not resolved from local-paths.conf." >&2
    echo "  Bound agent: ${MIND_AGENT:-(none)}" >&2
    echo "  Expected agents/<agent>/local-paths.conf with WORLD_PATH and META_PATH set." >&2
    echo "  /start Phase B writes this file; ensure A2 binding + B1-B7 paths completed first." >&2
    exit 2
fi

SCRIPT_PATH="$PROJECT_ROOT/core/scripts/permissions-add.py"
if [ ! -f "$SCRIPT_PATH" ]; then
    echo "ERROR: helper script not found: $SCRIPT_PATH" >&2
    exit 4
fi

# Pick the right Python launcher — same precedence as check-prerequisites.sh.
PY=""
if command -v py >/dev/null 2>&1 && py -3 --version >/dev/null 2>&1; then
    PY="py -3"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1 && python --version 2>&1 | grep -qE 'Python 3\.'; then
    PY="python"
fi

if [ -z "$PY" ]; then
    echo "ERROR: neither 'py -3' nor 'python3' nor 'python' (3.x) found on PATH." >&2
    echo "  Install Python 3.8+ — see core/scripts/check-prerequisites.sh." >&2
    exit 4
fi

eval "$PY \"$SCRIPT_PATH\" \"$WORLD_DIR\" \"$META_DIR\" \"$PROJECT_ROOT\""
exit $?
