#!/usr/bin/env bash
# Load the aspirations-precheck digest — returns path only if not already in context.
# Follows load-loop-digest.sh / load-iteration-close-digest.sh pattern for
# context-reads integration.
#
# The digest (core/config/aspirations-precheck-digest.md) holds the per-phase
# BODIES of the 19 `deferrable`-tier precheck sweeps. It is NOT the precheck
# spec: the phase tier TABLE stays in aspirations-precheck/SKILL.md and already
# carries each lane's exact Invocation, so the common path runs every deferrable
# sweep without loading this at all. Load it when a lane needs its full body —
# measured markers, parse-shape warnings, incident traces.
#
# Nothing always-run lives in the digest by construction (sentinel battery,
# cadence battery 0.5e, and the 0.5g.7 CNC drain stay inline) — moving an
# always-run trigger behind an on-demand read recreates . .
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
# _platform.sh MUST source before deriving paths — it normalizes REPO_ROOT on Windows/MSYS2
source "$CORE_ROOT/scripts/_platform.sh"

python3 "$CORE_ROOT/scripts/context-reads.py" check-file \
    "$CONFIG_DIR/aspirations-precheck-digest.md"
