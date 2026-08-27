#!/usr/bin/env bash
# Load CREATE_BLOCKER protocol digest — returns path only if not already in context.
# Follows load-execute-protocol.sh pattern for context-reads integration.
# The digest (core/config/create-blocker-protocol-digest.md) is the canonical
# spec for blocker creation — blocker-create-gate.py structural checks and
# capability-gate.py participant validation with explicit exit-code branches.
# Loaded on-demand when Phase 4.0 or Phase 4.1e needs to file a blocker.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/create-blocker-protocol-digest.md"
