#!/usr/bin/env bash
# Load cognitive primitives digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/cognitive-primitives-digest.md) describes the four
# LLM-file-able primitives: Investigate, Idea, Maintain, Cross-Agent Insight
# (Unblock routes through load-create-blocker-protocol.sh — separate digest).
# Loaded on-demand when the agent decides to file a primitive.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/cognitive-primitives-digest.md"
