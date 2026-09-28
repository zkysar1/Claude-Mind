#!/usr/bin/env bash
# init-mind.sh — Legacy wrapper for 4-tier initialization
#
# In the old architecture, mind/ held everything. Now:
#   world/         — Collective domain state  (init-world.sh)
#   <agent-name>/  — Per-agent private state  (init-agent.sh)
#   meta/          — Meta-strategies          (init-meta.sh)
#
# This wrapper calls all three for backward compatibility.
# New code should call init-world.sh + init-agent.sh + init-meta.sh directly.
#
# Usage:
#   MIND_AGENT=<name> bash core/scripts/init-mind.sh
#   bash core/scripts/init-mind.sh <agent-name>

set -euo pipefail

# Accept agent name as argument or from environment
AGENT_NAME_ARG="${1:-${MIND_AGENT:-}}"
if [ -z "$AGENT_NAME_ARG" ]; then
    echo "ERROR: Agent name required." >&2
    echo "Usage: bash core/scripts/init-mind.sh <agent-name>" >&2
    echo "   or: MIND_AGENT=<name> bash core/scripts/init-mind.sh" >&2
    exit 1
fi

# Bind BEFORE sourcing _paths.sh: it resolves the world and meta dirs from
# MIND_AGENT and exports them as MIND_WORLD/MIND_META, which every child reads
# first. Sourced earlier, a named agent inherited the caller's or the first
# agent's world ().
export MIND_AGENT="$AGENT_NAME_ARG"

source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"

echo "=== Full initialization (world + agent + meta) ==="
echo ""

# 1. Initialize collective domain state
bash "$CORE_ROOT/scripts/init-world.sh"
echo ""

# 2. Initialize per-agent private state
bash "$CORE_ROOT/scripts/init-agent.sh" "$AGENT_NAME_ARG"
echo ""

# 3. Initialize meta-strategies
bash "$CORE_ROOT/scripts/init-meta.sh"
echo ""

echo "=== Full initialization complete ==="

# 4. Birth-contract check (): advisory, and only for a RUNNING mind.
# The /start interview also runs this script (Phase C0), before it installs the
# hook slots, curriculum and self.md; agent-state is still absent then and turns
# RUNNING only at C9.9, so the gate skips it. A RUNNING loop gets here through
# /boot Phase -2 (on every /boot) or the entry battery's world_not_initialized
# row (only while a tier's .initialized marker is absent), so a run that skips
# /boot on an already-initialized mind never does. `|| true` because /boot
# aborts on a non-zero init, and a mind that lacks an element should boot and
# say so. Contract and measured coverage ():
# core/config/conventions/session-state.md "Pre-Landed Birth Contract".
if [ "$(bash "$CORE_ROOT/scripts/session-state-get.sh" || true)" = "RUNNING" ]; then
    echo ""
    python3 "$CORE_ROOT/scripts/birth-contract-check.py" "$AGENT_NAME_ARG" || true
fi
