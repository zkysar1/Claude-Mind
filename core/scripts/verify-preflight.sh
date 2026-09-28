#!/usr/bin/env bash
# verify-preflight.sh — a goal's mechanical verify checks in ONE call ().
#
#   bash core/scripts/verify-preflight.sh --goal <goal-id> --source <world|agent> \
#        [--artifact <file>]... [--source-file <path>] \
#        [--claim "<file-state claim>" --evidence "<in-turn Read/ls output>"] \
#        [--override-positive-state "<why>"] [--summary-file <path>] [--no-write] [--json]
#
# Runs the structured checks, the closure-evidence table, the artifact probe, the
# positive-state gate and the Q4 sample, makes the checkpoint + diary writes those
# aspirations-verify steps make, and prints ONE verdict listing every finding
# with its remedy.
#
# rc 0 = no check failed; 3 = at least one FAILED; 4 = none failed but one could
# not run (not a pass); 2 = usage. The exit code is the answer, so never pipe
# this (guard-1150). The WHY and the verdict vocabulary: verify-preflight.py.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/verify-preflight.py" "$@"
