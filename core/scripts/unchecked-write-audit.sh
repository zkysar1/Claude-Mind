#!/usr/bin/env bash
# unchecked-write-audit.sh — classify governed-store write call sites in skill
# pseudocode as VERIFIED / UNVERIFIED (hypothesis
# 2026-07-26_unchecked-writes-are-the-norm, ). Read-only: reads
# core/scripts/*.sh to derive the write-wrapper population and
# .claude/skills/*/SKILL.md to classify. Writes nothing.
# Shared measurement channel with the governed-store writer audit ().
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/unchecked-write-audit.py" "$@"
