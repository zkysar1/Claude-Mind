#!/usr/bin/env bash
# Load blocked-sleep recovery digest — returns path only if not already in context.
# Follows load-loop-digest.sh pattern for context-reads integration.
# Extracted from aspirations/SKILL.md Phase -0.5e to keep the orchestrator lean.
# Gated load — caller invokes this from one of two states:
#   Branch A: idle-tick.sh emitted "=== IDLE TICK ===" directive.
#   Branch B: idle-tick.sh returned empty AND blocked_sleep_until != null.
# Common iterations (no directive, no timer) never call it.
# DO NOT remove the context-reads.py check-file gate — the digest is ~2.5k
# tokens; without the dedup, every Branch A/B re-entry costs the full load.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh MUST source before deriving paths — it normalizes REPO_ROOT on Windows/MSYS2
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/blocked-sleep-recovery-digest.md"
