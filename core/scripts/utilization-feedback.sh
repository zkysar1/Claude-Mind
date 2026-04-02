#!/usr/bin/env bash
# Single-command utilization feedback — replaces Phase 4.26 LLM protocol.
# Reads retrieval-session.json (auto-written by retrieve.py) and applies
# times_helpful/times_noise increments to tree nodes and supplementary items.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/utilization-feedback.py" "$@"
