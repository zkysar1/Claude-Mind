#!/usr/bin/env bash
# missing-criteria-log.sh — thin wrapper (guard-365) over missing-criteria-log.py.
#
# Appends uncovered generated-checklist failures (aspirations-verify Q1.5 —
# BRD Gap 15 / , "TICKing All the Boxes" 2410.03608) to
# meta/missing-verification-criteria.jsonl. Reads a JSON record from stdin.
#
# STANDALONE — deliberately NOT daemon-routed (no /v1 endpoint). This is
# low-frequency verify-phase telemetry; a daemon endpoint would be scope creep
# (cf. .claude/rules/no-python-cli-fallback.md, which governs MIGRATED daemon
# wrappers, not net-new standalone scripts). Sourcing _paths.sh sets up the
# Windows python shim PATH so `python3` resolves, and exports the external
# paths the resolver reads.
set -euo pipefail

_SELF="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$_SELF/../.." && pwd)"

# shellcheck disable=SC1091
source "$PROJECT_ROOT/core/scripts/_paths.sh"

exec python3 "$PROJECT_ROOT/core/scripts/missing-criteria-log.py" "$@"
