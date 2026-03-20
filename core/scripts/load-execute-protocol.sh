#!/usr/bin/env bash
# Load execute protocol digest — returns path only if not already in context.
# Follows load-tree-summary.sh pattern for context-reads integration.
# The digest (core/config/execute-protocol-digest.md) is a compact version of
# .claude/skills/aspirations-execute/SKILL.md — retrieval protocol and
# post-execution steps only. ~136 lines vs ~636.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh MUST source before deriving paths — it normalizes REPO_ROOT on Windows/MSYS2
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/execute-protocol-digest.md"
