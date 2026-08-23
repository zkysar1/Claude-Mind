#!/usr/bin/env bash
# Load episode-chain protocol digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/episode-chain-protocol-digest.md) is the MR-Search
# retry-with-reflection pseudocode. Loaded on-demand after Phase 4-post
# classification when a deep-outcome failure is eligible for chaining.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/episode-chain-protocol-digest.md"
