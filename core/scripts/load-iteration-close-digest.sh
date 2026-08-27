#!/usr/bin/env bash
# Load iteration-close digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/iteration-close-digest.md) documents the LLM-residue
# checklist for the three per-iteration obligation phases (verify, state-update,
# learning-gate) that iteration-close.sh handles mechanically.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/iteration-close-digest.md"
