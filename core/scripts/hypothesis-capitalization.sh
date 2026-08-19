#!/usr/bin/env bash
# hypothesis-capitalization.sh — report BOTH hypothesis-pipeline health axes,
# ACCURACY and CAPITALIZATION, together (). Read-only: writes nothing.
#
# Deliberately NOT named *-ratchet.sh and deliberately NOT wired into
# meta/audit-baselines.yaml. Capitalization is a RATE, and that file's ratchet is
# one-way lower-is-better, so a capitalization COLLAPSE would report "ratcheted /
# OK" and permanently lower the bar. The full three-part refusal is in the
# module docstring of hypothesis-capitalization.py — read it before wiring this
# into a baseline.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/hypothesis-capitalization.py" "$@"
