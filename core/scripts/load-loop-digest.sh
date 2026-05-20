#!/usr/bin/env bash
# Load aspirations loop digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/aspirations-loop-digest.md) IS the per-iteration body
# for /aspirations loop — phase ordering, signal mutations, LOOP_CONTINUE
# contract. aspirations/SKILL.md is just the envelope (boot + session-end);
# the digest is the source of truth for the hot path.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh MUST source before deriving paths — it normalizes REPO_ROOT on Windows/MSYS2
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/aspirations-loop-digest.md"
