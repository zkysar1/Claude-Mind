#!/usr/bin/env bash
# domain-term-ratchet.sh — advisory ratchet over the core/domain border wall.
# Tracks registry-derived peer-deployment ids (core/config/environments/*.yaml)
# that are present in core files but absent from the domain-term blocklist
# ( outcome 5). Deliberately scoped to `environments/id`: the
# whole-universe census figure is ~89% conventions-heading prose and must not
# be ratcheted. A census that yields ZERO registry ids leaves the baseline
# UNTOUCHED (vacuous-run guard) rather than seeding 0.
# Exit 0 always unless VERIFY_LEARNING_DRIFT_HARD_GATE=1.
set -euo pipefail
source "$(cd "$(dirname "$0")" && pwd)/_paths.sh"
cd "$PROJECT_ROOT"
source "$CORE_ROOT/scripts/_platform.sh"
exec python3 "$CORE_ROOT/scripts/domain-term-ratchet.py" "$@"
